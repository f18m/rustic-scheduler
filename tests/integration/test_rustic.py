"""The scheduler driving the real, pinned Rustic binary on temporary repositories."""

import asyncio
import json
import shlex
import subprocess
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from helpers import metric
from rustic_scheduler import SUPPORTED_RUSTIC_VERSION
from rustic_scheduler.app import prepare, rustic_version
from rustic_scheduler.config import ConfigError
from rustic_scheduler.model import Operation, Outcome, Reason, Trigger
from rustic_scheduler.web import CSRF_HEADER, CSRF_VALUE, create_app

pytestmark = pytest.mark.integration


async def run(engine, job="docs", operation=Operation.BACKUP):
    accepted, message = engine.submit(job, operation, Trigger.MANUAL)
    assert accepted, message
    await engine.wait_idle()
    return engine.last_results.get((job, operation))


async def test_the_binary_is_the_pinned_version(real):
    assert await rustic_version(real.binary) == SUPPORTED_RUSTIC_VERSION


# ------------------------------------------------------- backup and progress


async def test_backup_with_live_json_progress_and_no_mqtt(real):
    (real.source / "big.bin").write_bytes(b"\0" * 8_000_000)
    engine = await real.engine()
    assert engine.mqtt is None and engine.notifier is None
    seen = []
    original = engine.metrics.set_progress
    engine.metrics.set_progress = lambda job, op, p: (seen.append(p), original(job, op, p))[1]

    result = await run(engine)

    assert result.outcome == Outcome.SUCCEEDED and result.exit_code == 0
    # Fields of Rustic's real `summary` message.
    assert result.data["files_new"] == 3 and result.data["files_changed"] == 0
    assert result.data["total_files_processed"] == 3
    total = sum(f.stat().st_size for f in real.source.iterdir())
    assert result.data["total_bytes_processed"] == total
    assert result.data["snapshots_saved"] == 1 and result.data["data_added_packed"] > 0
    # Fields of Rustic's real `status` messages.
    assert seen, "no progress message was parsed"
    assert all(p.bytes_total == total for p in seen)
    assert seen[-1].bytes_done == total and seen[-1].ratio == 1.0
    assert seen[0].eta_seconds is None  # Rustic omits the estimate when nothing is done yet
    # The snapshot exists and carries the job's label and host.
    (snap,) = real.snapshots()
    assert snap["label"] == "docs" and snap["hostname"] == "testhost"
    assert snap["id"].startswith(result.data["snapshot_ids"][0])
    assert snap["paths"] == [str(real.source)]
    # Diagnostics are in the run log, not in metric labels.
    log_text = Path(result.log_file).read_text()
    assert "summary" in log_text and "password is correct" in log_text
    assert "integration-test-password" not in log_text


async def test_several_sources_make_one_snapshot(real):
    second = real.root / "second"
    second.mkdir()
    (second / "c.txt").write_text("gamma")
    engine = await real.engine(
        real.config({"docs": real.job(sources=[str(real.source), str(second)])})
    )
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED
    (snap,) = real.snapshots()
    assert sorted(snap["paths"]) == sorted([str(real.source), str(second)])


async def test_missing_source_is_a_failed_backup(real):
    engine = await real.engine(
        real.config({"docs": real.job(sources=[str(real.root / "missing")])})
    )
    result = await run(engine)
    assert (
        result.outcome == Outcome.FAILED
        and result.reason == Reason.EXIT_CODE
        and result.exit_code == 1
    )
    assert "Not all snapshots were generated successfully" in result.detail
    assert real.snapshots() == []
    labels = {"job": "docs", "operation": "backup", "outcome": "failed"}
    assert metric(engine.metrics, "rustic_scheduler_operation_last_outcome", labels) == 1


async def test_wrong_password_is_a_failed_backup_and_is_not_logged(real):
    engine = await real.engine()
    real.password_file.write_text("not-the-password-123")
    result = await run(engine)
    assert result.outcome == Outcome.FAILED
    assert "not-the-password-123" not in result.detail
    assert "not-the-password-123" not in Path(result.log_file).read_text()


async def test_missing_password_reports_non_terminal_hint(real, monkeypatch, caplog):
    monkeypatch.delenv("RUSTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RUSTIC_PASSWORD_FILE", raising=False)
    real.profile("repo", f'[repository]\nrepository = "{real.repo}"\nno-cache = true\n')
    engine = await real.engine()

    result = await run(engine, operation=Operation.SNAPSHOTS)

    assert result.outcome == Outcome.FAILED and result.reason == Reason.EXIT_CODE
    assert "not a terminal" in result.detail
    assert "password-file" in result.detail
    assert "password-command" in caplog.text
    assert "password-file" in Path(result.log_file).read_text()

    real.profile(
        "repo",
        f'[repository]\nrepository = "{real.repo}"\n'
        f'password-file = "{real.password_file}"\nno-cache = true\n',
    )
    result = await run(await real.engine(), operation=Operation.SNAPSHOTS)
    assert result.outcome == Outcome.SUCCEEDED


async def test_unchanged_source_with_skip_if_unchanged_saves_no_snapshot(real):
    job = real.job(backup={"extra_args": ["--skip-if-unchanged"]})
    engine = await real.engine(real.config({"docs": job}))
    first = await run(engine)
    second = await run(engine)
    assert first.data["snapshots_saved"] == 1
    assert second.outcome == Outcome.SUCCEEDED and second.data["snapshots_saved"] == 0
    assert "no snapshot was saved" in second.detail
    assert len(real.snapshots()) == 1


async def test_shutdown_interrupts_a_real_backup_and_no_snapshot_appears(real):
    # A sparse file: large enough to still be in flight, yet free to create.
    with (real.source / "huge.bin").open("wb") as handle:
        handle.truncate(60 * 1024**3)
    engine = await real.engine()
    await engine.start()
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    for _ in range(100):
        await asyncio.sleep(0.1)
        if engine.progress.get(("docs", Operation.BACKUP)):
            break
    else:
        pytest.fail("the backup never reported progress")
    await asyncio.wait_for(engine.shutdown(), 30)
    result = engine.last_results[("docs", Operation.BACKUP)]
    # Rustic 0.11.4 exits with status 0 here; the scheduler must not call it success.
    assert result.outcome == Outcome.INTERRUPTED and result.reason == Reason.SHUTDOWN
    assert result.exit_code == 0
    assert real.snapshots() == []


# ----------------------------------------------------------------- profiles


async def test_multiple_real_profiles_are_combined_by_rustic(real):
    real.profile(
        "options",
        '[backup]\ntags = ["from-options"]\ndescription = "options profile"\nglobs = ["!*.tmp"]\n',
    )
    real.profile(
        "more",
        '[backup]\ndescription = "more profile"\nexclude-if-present = [".skip-me"]\n',
    )
    (real.source / "scratch.tmp").write_text("temporary")
    skipped = real.source / "cache"
    skipped.mkdir()
    (skipped / ".skip-me").write_text("")
    (skipped / "junk.txt").write_text("junk")

    job = real.job(profiles=["repo", "options", "more"])
    result = await run(await real.engine(real.config({"docs": job})))
    assert result.outcome == Outcome.SUCCEEDED

    (snap,) = real.snapshots()
    # repository + password came from `repo`, tags and globs from `options`,
    # exclude-if-present from `more`: all three files took effect together.
    assert snap["tags"] == ["from-options"]
    files = real.files_in(snap["id"])
    assert any(f.endswith("a.txt") for f in files)
    assert not any(f.endswith("scratch.tmp") for f in files)
    assert not any("junk.txt" in f for f in files)
    # Both profiles set `description`: the profile listed first wins.
    assert snap["description"] == "options profile"
    # The scheduler's own option is applied on top.
    assert snap["label"] == "docs"


async def test_profile_order_decides_conflicts(real):
    real.profile("options", '[backup]\ndescription = "options profile"\n')
    real.profile("more", '[backup]\ndescription = "more profile"\n')
    job = real.job(profiles=["repo", "more", "options"])
    await run(await real.engine(real.config({"docs": job})))
    assert real.snapshots()[0]["description"] == "more profile"


async def test_sources_can_come_from_a_profile(real):
    other = real.root / "other"
    other.mkdir()
    (other / "x.txt").write_text("x")
    real.profile(
        "sources",
        f'[[backup.snapshots]]\nsources = ["{real.source}"]\n\n[[backup.snapshots]]\nsources = ["{other}"]\n',
    )
    job = real.job(profiles=["repo", "sources"], sources=[])
    result = await run(await real.engine(real.config({"docs": job})))
    assert result.outcome == Outcome.SUCCEEDED
    assert result.data["snapshots_saved"] == 2 and result.data["summaries"] == 2
    assert [s["label"] for s in real.snapshots()] == ["docs", "docs"]


async def test_conflicting_profile_values_are_reported_before_anything_runs(real):
    real.profile("labelled", '[backup]\nlabel = "my-own-label"\n')
    with pytest.raises(ConfigError) as info:
        await prepare(real.config({"docs": real.job(profiles=["repo", "labelled"])}))
    message = str(info.value)
    assert "my-own-label" in message and "override" in message
    # Naming that label on the job resolves the conflict without touching the profile.
    job = real.job(profiles=["repo", "labelled"], label="my-own-label")
    result = await run(await real.engine(real.config({"docs": job})))
    assert result.outcome == Outcome.SUCCEEDED
    assert real.snapshots()[0]["label"] == "my-own-label"


async def test_invalid_profile_is_reported_with_rustics_own_message(real):
    real.profile("broken", "[backup]\nno-such-option = 1\n")
    with pytest.raises(ConfigError) as info:
        await prepare(real.config({"docs": real.job(profiles=["repo", "broken"])}))
    assert "jobs.docs" in str(info.value) and "no-such-option" in str(info.value)


async def test_profile_secrets_never_reach_the_error_message(real):
    real.profile("leaky", '[repository]\npassword = "plaintext-secret-xyz"\n[backup]\nbogus = 1\n')
    with pytest.raises(ConfigError) as info:
        await prepare(real.config({"docs": real.job(profiles=["leaky", "repo"])}))
    assert "plaintext-secret-xyz" not in str(info.value)


async def test_repository_identity_comes_from_rustic(real):
    real.profile(
        "same",
        f'[repository]\nrepository = "{real.repo}"\npassword-file = "{real.password_file}"\n',
    )
    real.profile(
        "elsewhere", f'[repository]\nrepository = "{real.root}/elsewhere"\npassword = "x"\n'
    )
    jobs = {
        "one": real.job(),
        "two": real.job(profiles=["same"]),
        "three": real.job(profiles=["elsewhere"]),
    }
    runtimes, _, _ = await prepare(real.config(jobs))
    assert runtimes["one"].lock_key == runtimes["two"].lock_key != runtimes["three"].lock_key


# ------------------------------------------------- telemetry, forget, prune


def shared_repo_jobs(real, **one):
    other = real.root / "other-source"
    other.mkdir(exist_ok=True)
    (other / "o.txt").write_text("other")
    return {
        "one": real.job(snapshots={"enabled": True}, **one),
        "two": real.job(sources=[str(other)], snapshots={"enabled": True}),
    }


async def test_latest_snapshot_size_and_file_count_cover_unchanged_files(real):
    """Rustic's snapshot summary counts every file of the snapshot, not just what was added."""
    source = real.root / "totals-source"
    (source / "sub").mkdir(parents=True)
    (source / "a.bin").write_bytes(b"a" * 1000)
    (source / "sub" / "b.bin").write_bytes(b"b" * 2500)
    (source / "c.txt").write_text("hi\n")
    job = real.job(sources=[str(source)], snapshots={"enabled": True})
    engine = await real.engine(real.config({"totals": job}))
    await run(engine, "totals")
    (source / "d.txt").write_text("more\n")  # one new file next to three unchanged ones
    backup = await run(engine, "totals")
    assert backup.data["files_new"] == 1 and backup.data["files_unmodified"] == 3

    files = [path for path in source.rglob("*") if path.is_file()]
    size = sum(path.stat().st_size for path in files)
    telemetry = engine.last_results[("totals", Operation.SNAPSHOTS)]
    assert telemetry.outcome == Outcome.SUCCEEDED
    assert telemetry.data["latest_files"] == len(files) == 4
    assert telemetry.data["latest_bytes"] == size == 3508
    assert metric(engine.metrics, "rustic_scheduler_snapshot_latest_files", {"job": "totals"}) == 4
    assert (
        metric(engine.metrics, "rustic_scheduler_snapshot_latest_bytes", {"job": "totals"}) == size
    )
    facts = engine.status()["jobs"]["totals"]["snapshots"]
    assert facts["latest_files"] == 4 and facts["latest_bytes"] == size and facts["count"] == 2


async def test_snapshot_telemetry_is_per_job_on_a_shared_repository(real):
    engine = await real.engine(real.config(shared_repo_jobs(real)))
    await run(engine, "one")
    await run(engine, "one")
    await run(engine, "two")
    assert len(real.snapshots()) == 3
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "one"}) == 2
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "two"}) == 1
    telemetry = engine.last_results[("one", Operation.SNAPSHOTS)]
    assert telemetry.outcome == Outcome.SUCCEEDED
    newest = real.snapshots("one")[-1]
    assert newest["id"].startswith(telemetry.data["latest_id"])
    latest = metric(
        engine.metrics, "rustic_scheduler_snapshot_latest_timestamp_seconds", {"job": "one"}
    )
    from rustic_scheduler.engine import parse_time

    assert latest == parse_time(newest["time"]).timestamp()
    assert (
        metric(engine.metrics, "rustic_scheduler_snapshot_latest_bytes", {"job": "one"})
        == (newest["summary"]["total_bytes_processed"])
    )
    assert (
        metric(
            engine.metrics, "rustic_scheduler_snapshot_collection_timestamp_seconds", {"job": "one"}
        )
        > 0
    )


async def test_forget_removes_only_the_jobs_own_snapshots(real):
    jobs = shared_repo_jobs(
        real,
        forget={
            "enabled": True,
            "keep": {"keep-last": 1},
            "after_backup": False,
            "schedule": {"days": "daily", "times": "04:00"},
        },
    )
    engine = await real.engine(real.config(jobs))
    for _ in range(3):
        await run(engine, "one")
        await run(engine, "two")
    assert len(real.snapshots("one")) == 3 and len(real.snapshots("two")) == 3

    result = await run(engine, "one", Operation.FORGET)

    assert result.outcome == Outcome.SUCCEEDED and not result.dry_run
    assert result.data["removed"] == 2 and result.data["kept"] == 1
    assert len(real.snapshots("one")) == 1
    assert len(real.snapshots("two")) == 3  # untouched
    assert metric(engine.metrics, "rustic_scheduler_forget_last_removed", {"job": "one"}) == 2
    # Forget is not enabled for job two, so it cannot even be requested.
    assert engine.trigger("two", Operation.FORGET)[0] is False


async def test_forget_is_off_by_default_and_dry_run_removes_nothing(real):
    engine = await real.engine()
    for _ in range(3):
        await run(engine)
    assert len(real.snapshots()) == 3  # no retention unless configured

    job = real.job(
        forget={"enabled": True, "keep": {"keep-last": 1}, "dry_run": True, "prune": True}
    )
    engine = await real.engine(real.config({"docs": job}))
    await run(engine)  # backup, then the after-backup forget preview
    result = engine.last_results[("docs", Operation.FORGET)]
    assert result.outcome == Outcome.SUCCEEDED and result.dry_run
    assert result.data["removed"] == 3 and result.data["kept"] == 1
    assert len(real.snapshots()) == 4
    assert metric(engine.metrics, "rustic_scheduler_forget_last_dry_run", {"job": "docs"}) == 1


async def test_forget_with_prune_and_standalone_prune_on_disposable_data(real):
    schedule = {"days": "daily", "times": "04:00"}
    job = real.job(
        forget={"enabled": True, "keep": {"keep-last": 1}, "prune": True},
        prune={"enabled": True, "schedule": schedule, "extra_args": ["--keep-delete", "0s"]},
        check={"enabled": True, "after_backup": True, "read_data": True},
    )
    engine = await real.engine(real.config({"docs": job}))
    for index in range(3):
        (real.source / "changing.bin").write_bytes(bytes([index]) * 3_000_000)
        result = await run(engine)
        assert result.outcome == Outcome.SUCCEEDED
    forget = engine.last_results[("docs", Operation.FORGET)]
    assert forget.outcome == Outcome.SUCCEEDED and forget.data["pruned"] is True
    assert "to repack" in Path(forget.log_file).read_text()  # Rustic's prune statistics
    assert len(real.snapshots()) == 1

    prune = await run(engine, operation=Operation.PRUNE)
    assert prune.outcome == Outcome.SUCCEEDED
    assert "--keep-delete" in Path(prune.log_file).read_text() or prune.exit_code == 0
    # The repository is still consistent and the remaining snapshot restorable.
    assert real.rustic("check", "--read-data").returncode == 0
    target = real.root / "restored"
    real.rustic("restore", "latest", str(target))
    restored = next(target.rglob("changing.bin"))
    assert restored.read_bytes() == bytes([2]) * 3_000_000


async def test_profile_forget_rules_can_be_used_explicitly(real):
    real.profile("retention", "[forget]\nkeep-last = 2\n")
    job = real.job(
        profiles=["repo", "retention"], forget={"enabled": True, "rules_from_profile": True}
    )
    engine = await real.engine(real.config({"docs": job}))
    for _ in range(4):
        await run(engine)
    assert len(real.snapshots()) == 2


async def test_profile_prune_setting_is_refused_unless_enabled_in_yaml(real):
    real.profile("pruning", "[forget]\nkeep-last = 2\nprune = true\n")
    job = real.job(
        profiles=["repo", "pruning"], forget={"enabled": True, "rules_from_profile": True}
    )
    with pytest.raises(ConfigError, match="prune = true"):
        await prepare(real.config({"docs": job}))


# -------------------------------------------------------------------- check


async def test_check_passes_then_detects_real_corruption(real):
    job = real.job(
        check={"enabled": True, "after_backup": True, "read_data": True},
        forget={"enabled": True, "keep": {"keep-last": 1}},
    )
    (real.source / "payload.bin").write_bytes(bytes(range(256)) * 20_000)
    engine = await real.engine(real.config({"docs": job}))
    await run(engine)
    check = engine.last_results[("docs", Operation.CHECK)]
    assert check.outcome == Outcome.SUCCEEDED and check.trigger == Trigger.AFTER_BACKUP

    # Damage the largest pack file of the throwaway repository.
    pack = max(
        (p for p in (real.repo / "data").rglob("*") if p.is_file()), key=lambda p: p.stat().st_size
    )
    data = bytearray(pack.read_bytes())
    data[len(data) // 2 : len(data) // 2 + 64] = b"\xff" * 64
    pack.chmod(0o600)
    pack.write_bytes(data)

    await run(engine)
    check = engine.last_results[("docs", Operation.CHECK)]
    assert check.outcome == Outcome.FAILED and check.reason == Reason.EXIT_CODE
    # A failed check after the backup stops forget from deleting older snapshots.
    assert engine.last_skips[("docs", Operation.FORGET)].reason == Reason.BACKUP_NOT_SUCCESSFUL
    assert len(real.snapshots()) == 2
    labels = {"job": "docs", "operation": "check", "outcome": "failed"}
    assert metric(engine.metrics, "rustic_scheduler_operation_last_outcome", labels) == 1


async def test_metadata_check_does_not_read_pack_data(real):
    job = real.job(check={"enabled": True, "after_backup": True})
    engine = await real.engine(real.config({"docs": job}))
    await run(engine)
    check = engine.last_results[("docs", Operation.CHECK)]
    assert check.outcome == Outcome.SUCCEEDED
    assert "--read-data" not in json.dumps(check.to_dict())
    assert "reading pack" not in Path(check.log_file).read_text().lower()


# ------------------------------------------------------------- concurrency


async def test_two_jobs_on_one_real_repository_run_one_after_the_other(real):
    engine = await real.engine(real.config(shared_repo_jobs(real)))
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    engine.submit("two", Operation.BACKUP, Trigger.SCHEDULE)
    await engine.wait_idle()
    one = engine.last_results[("one", Operation.BACKUP)]
    one_telemetry = engine.last_results[("one", Operation.SNAPSHOTS)]
    two = engine.last_results[("two", Operation.BACKUP)]
    assert one.outcome == two.outcome == Outcome.SUCCEEDED
    assert one_telemetry.finished_at <= two.started_at  # the whole pipeline of `one` finished first
    assert real.rustic("check").returncode == 0


# ------------------------------------------------------------ web interface


async def test_web_interface_starts_real_runs_and_its_manual_commands_work(real, short_tmp):
    """A backup and a check from the page, then the commands its panel prints, run as printed."""
    config = real.config(
        {"docs": real.job(check={"enabled": True}), "other": real.job(label="other")},
        web={"enabled": True},
    )
    engine = await real.engine(config)
    runner = web.AppRunner(create_app(engine, config.web, SUPPORTED_RUSTIC_VERSION))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as session:
            for job, operation in (("docs", "backup"), ("other", "backup"), ("docs", "check")):
                async with session.post(
                    f"/api/v1/jobs/{job}/{operation}/run", headers={CSRF_HEADER: CSRF_VALUE}
                ) as response:
                    assert response.status == 202
                await engine.wait_idle()
            async with session.get("/api/v1/overview") as response:
                data = await response.json()
    finally:
        await runner.cleanup()

    assert [(h["job"], h["operation"], h["outcome"], h["trigger"]) for h in data["history"]] == [
        ("docs", "check", "succeeded", "web"),
        ("other", "backup", "succeeded", "web"),
        ("docs", "backup", "succeeded", "web"),
    ]
    assert "integration-test-password" not in json.dumps(data)

    # The "plain Rustic" commands of the panel, with the real binary in place of `rustic`.
    manual = next(job for job in data["jobs"] if job["name"] == "docs")["manual"]
    assert manual["scope"] == "--filter-label docs --filter-host testhost"
    base = [real.binary, *shlex.split(manual["profiles"])]

    def rustic(*args: str) -> str:
        done = subprocess.run([*base, *args], env=real.env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout

    groups = json.loads(rustic("snapshots", "--json", *shlex.split(manual["scope"])))
    (snap,) = [s for g in groups for s in g["snapshots"]]  # only this job's, not `other`'s
    assert snap["label"] == "docs"
    assert "a.txt" in rustic("ls", snap["id"])
    rustic("check")
    target = short_tmp / "restored"
    rustic("restore", "--dry-run", snap["id"], str(target))
    # Rustic 0.11.4 creates the empty target directory even for a dry run, and nothing else.
    assert not target.exists() or list(target.iterdir()) == []
    rustic("restore", f"{snap['id']}:{real.source}", str(target))
    assert (target / "a.txt").read_text() == "alpha\n"
