"""Engine behaviour against a scripted Rustic stand-in."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from helpers import STATUS, SUMMARY, forget_json, metric, snapshot, snapshots_json
from rustic_scheduler.model import Operation, Outcome, Reason, Trigger

OK_BACKUP = {"stdout": [STATUS, SUMMARY]}


async def run(engine, job="docs", operation=Operation.BACKUP, trigger=Trigger.MANUAL):
    accepted, message = engine.submit(job, operation, trigger)
    assert accepted, message
    await engine.wait_idle()
    return engine.last_results.get((job, operation))


def outcome_metric(engine, job, operation, outcome):
    labels = {"job": job, "operation": operation, "outcome": outcome}
    return metric(engine.metrics, "rustic_scheduler_operation_last_outcome", labels)


# ----------------------------------------------------------------- outcomes


async def test_backup_succeeds_only_with_a_summary(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP})
    engine = await sandbox.engine()
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED and result.exit_code == 0
    assert result.data["files_new"] == 3 and result.data["snapshots_saved"] == 1
    assert outcome_metric(engine, "docs", "backup", "succeeded") == 1
    assert outcome_metric(engine, "docs", "backup", "failed") == 0
    assert metric(engine.metrics, "rustic_scheduler_backup_last_files_new", {"job": "docs"}) == 3


async def test_exit_zero_without_summary_is_unknown_not_success(sandbox):
    sandbox.write_plan({"backup": {"stdout": [STATUS], "exit": 0}})
    result = await run(await sandbox.engine())
    assert result.outcome == Outcome.UNKNOWN and result.reason == Reason.NO_SUMMARY


async def test_nonzero_exit_is_failed_with_the_error_as_detail(sandbox):
    stderr = ["[INFO] starting", "[ERROR] error backing up /data: permission denied"]
    sandbox.write_plan({"backup": {"exit": 1, "stderr": stderr}})
    engine = await sandbox.engine()
    result = await run(engine)
    assert result.outcome == Outcome.FAILED and result.reason == Reason.EXIT_CODE
    assert result.exit_code == 1 and "permission denied" in result.detail
    labels = {"job": "docs", "operation": "backup"}
    assert metric(engine.metrics, "rustic_scheduler_operation_last_exit_code", labels) == 1
    assert (
        metric(engine.metrics, "rustic_scheduler_operation_last_success_timestamp_seconds", labels)
        is None
    )


async def test_non_terminal_error_in_backup_result(sandbox):
    sandbox.write_plan({"backup": {"exit": 1, "stderr": ["error: IO error: not a terminal"]}})
    result = await run(await sandbox.engine())
    assert result.outcome == Outcome.FAILED and result.reason == Reason.EXIT_CODE
    assert "not a terminal" in result.detail
    assert "password-file" in result.detail


async def test_non_terminal_hint_survives_long_stderr(sandbox):
    stderr = ["error: IO error: not a terminal"] + [f"[INFO] line {i}" for i in range(50)]
    sandbox.write_plan({"backup": {"exit": 1, "stderr": stderr}})
    result = await run(await sandbox.engine())
    assert result.outcome == Outcome.FAILED
    assert "password-file" in result.detail
    assert len(result.detail) <= 600


async def test_summary_but_nonzero_exit_is_still_failed(sandbox):
    # One of several snapshots failed: Rustic prints a summary and exits 1.
    sandbox.write_plan({"backup": {"stdout": [SUMMARY], "exit": 1}})
    assert (await run(await sandbox.engine())).outcome == Outcome.FAILED


async def test_interrupted_rustic_exits_zero_and_is_reported_interrupted(sandbox):
    stderr = ["[INFO] Ctrl-C received, shutting down..."]
    sandbox.write_plan({"backup": {"stdout": [STATUS], "stderr": stderr, "exit": 0}})
    result = await run(await sandbox.engine())
    assert result.outcome == Outcome.INTERRUPTED and result.reason == Reason.SIGNAL


async def test_timeout_stops_rustic_and_fails(sandbox):
    sandbox.write_plan({"backup": {"sleep": 30}})
    config = sandbox.config(
        {"docs": {"profiles": ["repo"], "sources": ["/d"], "backup": {"timeout": 0.3}}}
    )
    result = await run(await sandbox.engine(config))
    assert result.outcome == Outcome.FAILED and result.reason == Reason.TIMEOUT
    assert [e["event"] for e in sandbox.journal(None)] == ["start", "interrupted"]


async def test_missing_binary_is_a_failed_run(sandbox):
    engine = await sandbox.engine()
    runtime = engine.runtimes["docs"]
    engine.runtimes["docs"] = type(runtime)(**{**runtime.__dict__, "binary": "/nonexistent/rustic"})
    result = await run(engine)
    assert result.outcome == Outcome.FAILED and result.reason == Reason.SPAWN_ERROR


async def test_queued_is_never_reported_as_a_result(sandbox):
    sandbox.write_plan({"backup": {"sleep": 0.5, "stdout": [SUMMARY]}})
    engine = await sandbox.engine()
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await asyncio.sleep(0.2)
    assert ("docs", Operation.BACKUP) not in engine.last_results
    assert (
        metric(
            engine.metrics,
            "rustic_scheduler_operation_running",
            {"job": "docs", "operation": "backup"},
        )
        == 1
    )
    assert outcome_metric(engine, "docs", "backup", "succeeded") is None
    await engine.wait_idle()
    assert outcome_metric(engine, "docs", "backup", "succeeded") == 1


async def test_retries_only_after_plain_failures(sandbox):
    sandbox.write_plan({"backup": [{"exit": 1}, {"exit": 1}, OK_BACKUP]})
    job = {"profiles": ["repo"], "sources": ["/d"], "backup": {"retries": 2, "retry_delay": 0.05}}
    engine = await sandbox.engine(sandbox.config({"docs": job}))
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED and result.attempts == 3
    runs = {
        "job": "docs",
        "operation": "backup",
        "trigger": "manual",
        "outcome": "succeeded",
        "reason": "none",
    }
    assert (
        metric(engine.metrics, "rustic_scheduler_operation_runs_total", runs) == 1
    )  # one run, not three


async def test_no_retry_after_timeout(sandbox):
    sandbox.write_plan({"backup": {"sleep": 30}})
    job = {
        "profiles": ["repo"],
        "sources": ["/d"],
        "backup": {"retries": 3, "timeout": 0.2, "retry_delay": 0},
    }
    result = await run(await sandbox.engine(sandbox.config({"docs": job})))
    assert result.attempts == 1 and len(sandbox.journal()) == 1


# ----------------------------------------------------------------- progress


async def test_progress_is_exported_live_and_unknown_values_are_absent(sandbox):
    partial = {"message_type": "status", "seconds_elapsed": 1, "bytes_done": 10}
    sandbox.write_plan({"backup": {"stdout": [partial, SUMMARY], "line_delay": 0.4}})
    engine = await sandbox.engine()
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await asyncio.sleep(0.3)
    labels = {"job": "docs", "operation": "backup"}
    assert metric(engine.metrics, "rustic_scheduler_progress_bytes_done", labels) == 10
    # Rustic did not report these: they must be absent, not zero.
    assert metric(engine.metrics, "rustic_scheduler_progress_bytes_total", labels) is None
    assert metric(engine.metrics, "rustic_scheduler_progress_ratio", labels) is None
    assert metric(engine.metrics, "rustic_scheduler_progress_eta_seconds", labels) is None
    assert (
        engine.status()["jobs"]["docs"]["operations"]["backup"]["progress"]["bytes_total"] is None
    )
    await engine.wait_idle()
    assert metric(engine.metrics, "rustic_scheduler_progress_bytes_done", labels) is None


# ----------------------------------------------------------------- ordering


def full_job(**overrides):
    job = {
        "profiles": ["repo"],
        "sources": ["/d"],
        "snapshots": {"enabled": True},
        "forget": {"enabled": True, "keep": {"keep-last": 1}},
        "check": {"enabled": True, "after_backup": True},
    }
    return job | overrides


def forget_plan(removed=1):
    old = snapshot("docs", "2026-01-01T00:00:00Z", "c")
    new = snapshot("docs", "2026-01-02T00:00:00Z", "d")
    items = [(new, True)] + [(old, False)] * removed
    return {"stdout": [forget_json(*items)]}


async def test_after_backup_order_is_check_forget_snapshots(sandbox):
    sandbox.write_plan(
        {
            "backup": OK_BACKUP,
            "forget": forget_plan(),
            "snapshots": {"stdout": [snapshots_json(snapshot("docs", "2026-01-02T00:00:00Z"))]},
        }
    )
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    await run(engine)
    assert sandbox.commands() == ["backup", "check", "forget", "forget", "snapshots"]
    forget_runs = [e["argv"] for e in sandbox.journal() if e["command"] == "forget"]
    assert "--dry-run" in forget_runs[0] and "--dry-run" not in forget_runs[1]
    for operation in (Operation.CHECK, Operation.FORGET, Operation.SNAPSHOTS):
        result = engine.last_results[("docs", operation)]
        assert result.outcome == Outcome.SUCCEEDED and result.trigger == Trigger.AFTER_BACKUP
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "docs"}) == 1
    assert metric(engine.metrics, "rustic_scheduler_forget_last_removed", {"job": "docs"}) == 1


async def test_failed_backup_skips_forget_and_check_but_still_collects_telemetry(sandbox):
    sandbox.write_plan({"backup": {"exit": 1}, "snapshots": {"stdout": [snapshots_json()]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    result = await run(engine)
    assert result.outcome == Outcome.FAILED
    assert sandbox.commands() == ["backup", "snapshots"]
    for operation in (Operation.FORGET, Operation.CHECK):
        skip = engine.last_skips[("docs", operation)]
        assert skip.outcome == Outcome.SKIPPED and skip.reason == Reason.BACKUP_NOT_SUCCESSFUL
        assert outcome_metric(engine, "docs", operation.value, "skipped") == 1
    assert engine.last_results[("docs", Operation.SNAPSHOTS)].outcome == Outcome.SUCCEEDED


async def test_failed_check_blocks_forget(sandbox):
    sandbox.write_plan(
        {"backup": OK_BACKUP, "check": {"exit": 1}, "snapshots": {"stdout": [snapshots_json()]}}
    )
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    await run(engine)
    assert sandbox.commands() == ["backup", "check", "snapshots"]
    assert engine.last_results[("docs", Operation.BACKUP)].outcome == Outcome.SUCCEEDED
    assert engine.last_results[("docs", Operation.CHECK)].outcome == Outcome.FAILED
    assert ("docs", Operation.FORGET) in engine.last_skips


async def test_telemetry_failure_does_not_change_the_backup_result(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP, "snapshots": {"exit": 1}})
    job = {"profiles": ["repo"], "sources": ["/d"], "snapshots": {"enabled": True}}
    engine = await sandbox.engine(sandbox.config({"docs": job}))
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED
    assert engine.last_results[("docs", Operation.SNAPSHOTS)].outcome == Outcome.FAILED
    assert (
        metric(engine.metrics, "rustic_scheduler_snapshot_collection_errors_total", {"job": "docs"})
        == 1
    )
    assert (
        metric(
            engine.metrics,
            "rustic_scheduler_snapshot_collection_timestamp_seconds",
            {"job": "docs"},
        )
        is None
    )


async def test_disabled_operations_never_run(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP})
    engine = await sandbox.engine()
    await run(engine)
    assert sandbox.commands() == ["backup"]


# ---------------------------------------------------------- forget safeguards


async def test_manual_forget_and_prune_are_refused_when_not_enabled(sandbox):
    engine = await sandbox.engine()
    for operation in (Operation.FORGET, Operation.PRUNE):
        accepted, message = engine.trigger("docs", operation)
        assert not accepted and "not enabled" in message
    await engine.wait_idle()
    assert sandbox.commands() == []
    assert engine.trigger("nope", Operation.BACKUP) == (False, "unknown job 'nope'")


async def test_forget_refuses_when_preview_selects_foreign_snapshots(sandbox):
    foreign = snapshot("someone-else", "2026-01-01T00:00:00Z")
    sandbox.write_plan({"forget": {"stdout": [forget_json((foreign, False))]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    result = await run(engine, operation=Operation.FORGET)
    assert result.outcome == Outcome.FAILED and result.reason == Reason.SCOPE_VIOLATION
    assert "nothing was removed" in result.detail
    assert len(sandbox.journal()) == 1 and "--dry-run" in sandbox.journal()[0]["argv"]


async def test_forget_checks_the_host_when_one_is_configured(sandbox):
    other_host = snapshot("docs", "2026-01-01T00:00:00Z", host="laptop")
    sandbox.write_plan({"forget": {"stdout": [forget_json((other_host, False))]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job(host="nas")}))
    result = await run(engine, operation=Operation.FORGET)
    assert result.reason == Reason.SCOPE_VIOLATION


async def test_forget_max_remove_and_remove_everything_guards(sandbox):
    sandbox.write_plan({"forget": forget_plan(removed=5)})
    job = full_job(forget={"enabled": True, "keep": {"keep-last": 1}, "max_remove": 2})
    result = await run(
        await sandbox.engine(sandbox.config({"docs": job})), operation=Operation.FORGET
    )
    assert result.outcome == Outcome.FAILED and result.reason == Reason.REMOVAL_LIMIT
    assert len(sandbox.journal()) == 1  # only the preview ran

    sandbox.journal_file.unlink()
    only = snapshot("docs", "2026-01-01T00:00:00Z")
    sandbox.write_plan({"forget": {"stdout": [forget_json((only, False))]}})
    result = await run(
        await sandbox.engine(sandbox.config({"docs": full_job()})), operation=Operation.FORGET
    )
    assert result.reason == Reason.REMOVAL_LIMIT and "every snapshot" in result.detail
    assert len(sandbox.journal()) == 1


async def test_forget_dry_run_never_removes(sandbox):
    sandbox.write_plan({"forget": forget_plan(removed=2)})
    job = full_job(
        forget={"enabled": True, "keep": {"keep-last": 1}, "dry_run": True, "prune": True}
    )
    engine = await sandbox.engine(sandbox.config({"docs": job}))
    result = await run(engine, operation=Operation.FORGET)
    assert result.outcome == Outcome.SUCCEEDED and result.dry_run and result.data["removed"] == 2
    runs = sandbox.journal()
    assert len(runs) == 1 and "--dry-run" in runs[0]["argv"] and "--prune" not in runs[0]["argv"]
    assert metric(engine.metrics, "rustic_scheduler_forget_last_dry_run", {"job": "docs"}) == 1


async def test_forget_with_nothing_to_remove_does_not_run_for_real(sandbox):
    sandbox.write_plan({"forget": forget_plan(removed=0)})
    result = await run(
        await sandbox.engine(sandbox.config({"docs": full_job()})), operation=Operation.FORGET
    )
    assert result.outcome == Outcome.SUCCEEDED and result.data["removed"] == 0
    assert len(sandbox.journal()) == 1


async def test_prune_flag_appears_only_when_enabled(sandbox):
    sandbox.write_plan({"forget": forget_plan()})
    await run(
        await sandbox.engine(sandbox.config({"docs": full_job()})), operation=Operation.FORGET
    )
    assert all("--prune" not in e["argv"] for e in sandbox.journal())
    sandbox.journal_file.unlink()
    job = full_job(forget={"enabled": True, "keep": {"keep-last": 1}, "prune": True})
    await run(await sandbox.engine(sandbox.config({"docs": job})), operation=Operation.FORGET)
    assert ["--prune" in e["argv"] for e in sandbox.journal()] == [False, True]


async def test_unreadable_forget_preview_removes_nothing(sandbox):
    sandbox.write_plan({"forget": {"stdout": ["this is not json"]}})
    result = await run(
        await sandbox.engine(sandbox.config({"docs": full_job()})), operation=Operation.FORGET
    )
    assert result.outcome == Outcome.FAILED and result.reason == Reason.BAD_OUTPUT
    assert len(sandbox.journal()) == 1


async def test_snapshot_telemetry_rejects_foreign_snapshots(sandbox):
    mixed = snapshots_json(
        snapshot("docs", "2026-01-01T00:00:00Z"), snapshot("other", "2026-01-01T00:00:00Z")
    )
    sandbox.write_plan({"snapshots": {"stdout": [mixed]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    result = await run(engine, operation=Operation.SNAPSHOTS)
    assert result.reason == Reason.SCOPE_VIOLATION
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "docs"}) is None


async def test_snapshot_telemetry_values(sandbox):
    listing = snapshots_json(
        snapshot("docs", "2026-01-01T10:00:00.123456789+01:00", "a"),
        snapshot("docs", "2026-01-03T10:00:00+01:00", "b"),
    )
    sandbox.write_plan({"snapshots": {"stdout": [listing]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    result = await run(engine, operation=Operation.SNAPSHOTS)
    assert result.data["count"] == 2 and result.data["latest_id"] == "b" * 8
    latest = datetime(2026, 1, 3, 9, 0, tzinfo=UTC).timestamp()
    assert (
        metric(
            engine.metrics, "rustic_scheduler_snapshot_latest_timestamp_seconds", {"job": "docs"}
        )
        == latest
    )
    assert metric(engine.metrics, "rustic_scheduler_snapshot_latest_bytes", {"job": "docs"}) == 4242
    args = sandbox.journal()[0]["argv"]
    assert args[args.index("--filter-label") + 1] == "docs"


async def test_empty_scope_reports_zero_snapshots_and_no_timestamp(sandbox):
    sandbox.write_plan({"snapshots": {"stdout": [snapshots_json()]}})
    engine = await sandbox.engine(sandbox.config({"docs": full_job()}))
    await run(engine, operation=Operation.SNAPSHOTS)
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "docs"}) == 0
    assert (
        metric(
            engine.metrics, "rustic_scheduler_snapshot_latest_timestamp_seconds", {"job": "docs"}
        )
        is None
    )


# -------------------------------------------------------------- concurrency


def two_jobs(**extra):
    return {
        "one": {"profiles": ["repo"], "sources": ["/a"]} | extra,
        "two": {"profiles": ["repo"], "sources": ["/b"]} | extra,
    }


async def test_jobs_on_the_same_repository_never_overlap(sandbox):
    sandbox.write_plan({"backup": {"sleep": 0.4, "stdout": [SUMMARY]}})
    engine = await sandbox.engine(sandbox.config(two_jobs()))
    assert engine.runtimes["one"].lock_key == engine.runtimes["two"].lock_key
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    engine.submit("two", Operation.BACKUP, Trigger.SCHEDULE)
    await asyncio.sleep(0.2)
    assert (
        metric(
            engine.metrics,
            "rustic_scheduler_operation_queued",
            {"job": "two", "operation": "backup"},
        )
        == 1
    )
    assert engine.status()["jobs"]["two"]["operations"]["backup"]["state"] == "queued"
    await engine.wait_idle()
    events = [(e["event"], e["argv"][-1]) for e in sandbox.journal(None)]
    assert events == [("start", "/a"), ("end", "/a"), ("start", "/b"), ("end", "/b")]
    assert engine.last_results[("two", Operation.BACKUP)].outcome == Outcome.SUCCEEDED


async def test_jobs_on_different_repositories_run_in_parallel(sandbox):
    sandbox.profile("other", f'[repository]\nrepository = "{sandbox.root}/other"\n')
    plan = {"backup": {"sleep": 0.4, "stdout": [SUMMARY]}}
    plan["show-config"] = [
        {"toml": '[repository]\nrepository = "/r1"\n'},
        {"toml": '[repository]\nrepository = "/r2"\n'},
    ]
    sandbox.write_plan(plan)
    jobs = two_jobs()
    jobs["two"]["profiles"] = ["other"]
    engine = await sandbox.engine(sandbox.config(jobs))
    assert engine.runtimes["one"].lock_key != engine.runtimes["two"].lock_key
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    engine.submit("two", Operation.BACKUP, Trigger.SCHEDULE)
    await engine.wait_idle()
    events = [e["event"] for e in sandbox.journal(None)]
    assert events == ["start", "start", "end", "end"]


async def test_a_job_still_running_makes_its_next_run_skipped(sandbox):
    sandbox.write_plan({"backup": {"sleep": 0.4, "stdout": [SUMMARY]}})
    engine = await sandbox.engine()
    assert engine.submit("docs", Operation.BACKUP, Trigger.SCHEDULE)[0]
    await asyncio.sleep(0.15)
    accepted, message = engine.submit("docs", Operation.BACKUP, Trigger.SCHEDULE)
    assert not accepted and "already running" in message
    skip = engine.last_skips[("docs", Operation.BACKUP)]
    assert skip.outcome == Outcome.SKIPPED and skip.reason == Reason.ALREADY_RUNNING
    await engine.wait_idle()
    assert len(sandbox.journal()) == 1
    skipped = {
        "job": "docs",
        "operation": "backup",
        "trigger": "schedule",
        "outcome": "skipped",
        "reason": "already_running",
    }
    assert metric(engine.metrics, "rustic_scheduler_operation_runs_total", skipped) == 1
    # The skip must not hide the outcome of the run that was going on.
    assert outcome_metric(engine, "docs", "backup", "succeeded") == 1


async def test_waiting_too_long_for_the_repository_is_a_skip(sandbox):
    sandbox.write_plan({"backup": {"sleep": 0.8, "stdout": [SUMMARY]}})
    engine = await sandbox.engine(sandbox.config(two_jobs(repository_wait_timeout=0.2)))
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    engine.submit("two", Operation.BACKUP, Trigger.SCHEDULE)
    await engine.wait_idle()
    assert engine.last_results[("one", Operation.BACKUP)].outcome == Outcome.SUCCEEDED
    assert ("two", Operation.BACKUP) not in engine.last_results
    assert engine.last_skips[("two", Operation.BACKUP)].reason == Reason.REPOSITORY_BUSY
    assert len(sandbox.journal()) == 1


async def test_manual_run_queues_behind_scheduled_work_on_the_same_repository(sandbox):
    sandbox.write_plan({"backup": {"sleep": 0.3, "stdout": [SUMMARY]}})
    engine = await sandbox.engine(sandbox.config(two_jobs()))
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    await asyncio.sleep(0.1)
    assert engine.trigger("two", Operation.BACKUP) == (True, "queued")
    assert engine.trigger("one", Operation.BACKUP)[0] is False  # same job: refused, not doubled
    await engine.wait_idle()
    assert engine.last_results[("two", Operation.BACKUP)].trigger == Trigger.MANUAL
    assert [e["argv"][-1] for e in sandbox.journal()] == ["/a", "/b"]


async def test_external_holder_of_the_lock_file_blocks_the_scheduler(sandbox):
    import fcntl
    import os

    sandbox.write_plan({"backup": OK_BACKUP})
    engine = await sandbox.engine()
    lock_dir = sandbox.state_dir / "locks"
    fd = os.open(lock_dir / f"{engine.runtimes['docs'].lock_key}.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    engine.locks._poll = 0.05
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await asyncio.sleep(0.3)
    assert sandbox.journal() == []  # e.g. `rustic-scheduler rustic docs -- prune` is running
    os.close(fd)
    await engine.wait_idle()
    assert engine.last_results[("docs", Operation.BACKUP)].outcome == Outcome.SUCCEEDED


# ----------------------------------------------------- shutdown and recovery


async def test_shutdown_interrupts_the_running_backup_and_drops_queued_work(sandbox):
    sandbox.write_plan({"backup": {"sleep": 30, "stdout": [STATUS]}})
    engine = await sandbox.engine(sandbox.config(two_jobs(snapshots={"enabled": True})))
    await engine.start()
    engine.submit("one", Operation.BACKUP, Trigger.SCHEDULE)
    engine.submit("two", Operation.BACKUP, Trigger.SCHEDULE)
    await asyncio.sleep(0.3)
    await asyncio.wait_for(engine.shutdown(), 10)
    one = engine.last_results[("one", Operation.BACKUP)]
    assert one.outcome == Outcome.INTERRUPTED and one.reason == Reason.SHUTDOWN
    assert engine.last_skips[("two", Operation.BACKUP)].reason == Reason.SHUTDOWN
    assert engine.last_skips[("one", Operation.SNAPSHOTS)].reason == Reason.SHUTDOWN
    assert [e["event"] for e in sandbox.journal(None)] == ["start", "interrupted"]
    assert not engine.ready
    assert engine.submit("one", Operation.BACKUP, Trigger.MANUAL)[0] is False


async def test_rustic_that_ignores_sigterm_is_killed_after_the_grace_period(sandbox):
    sandbox.write_plan({"backup": {"sleep": 30, "ignore_term": True}})
    engine = await sandbox.engine(sandbox.config(scheduler={"stop_grace": 0.3}))
    await engine.start()
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await asyncio.sleep(0.3)
    await asyncio.wait_for(engine.shutdown(), 10)
    result = engine.last_results[("docs", Operation.BACKUP)]
    assert result.outcome == Outcome.INTERRUPTED and result.exit_code == -9


async def test_a_run_cut_short_by_a_crash_is_unknown_after_restart(sandbox):
    sandbox.write_plan({"backup": {"sleep": 30}})
    engine = await sandbox.engine()
    engine.submit("docs", Operation.BACKUP, Trigger.SCHEDULE)
    await asyncio.sleep(0.3)
    state = json.loads((sandbox.state_dir / "state.json").read_text())
    assert "docs/backup" in state["running"]
    # Simulate a crash: the scheduler disappears without recording a result.
    for task in list(engine._tasks):
        task.cancel()
    await asyncio.sleep(0.2)
    (sandbox.state_dir / "state.json").write_text(json.dumps(state))

    sandbox.write_plan({"backup": OK_BACKUP})
    restarted = await sandbox.engine()
    await restarted.start()
    result = restarted.last_results[("docs", Operation.BACKUP)]
    assert result.outcome == Outcome.UNKNOWN and result.reason == Reason.SCHEDULER_RESTART
    assert outcome_metric(restarted, "docs", "backup", "unknown") == 1
    assert outcome_metric(restarted, "docs", "backup", "succeeded") == 0
    await restarted.shutdown()


async def test_last_results_survive_a_restart(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP})
    await run(await sandbox.engine())
    restarted = await sandbox.engine()
    await restarted.start()
    assert restarted.last_results[("docs", Operation.BACKUP)].outcome == Outcome.SUCCEEDED
    assert outcome_metric(restarted, "docs", "backup", "succeeded") == 1
    assert metric(restarted.metrics, "rustic_scheduler_backup_last_files_new", {"job": "docs"}) == 3
    await restarted.shutdown()


async def test_no_state_directory_still_works(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP})
    engine = await sandbox.engine(sandbox.config(scheduler={"state_dir": None}))
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED and result.log_file is None
    assert not sandbox.state_dir.exists()


# --------------------------------------------------------------- scheduling


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


SCHEDULED = {
    "profiles": ["repo"],
    "sources": ["/d"],
    "schedule": {"days": "daily", "times": ["02:00"]},
}
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


async def scheduled_engine(sandbox, job=None, **scheduler):
    sandbox.write_plan({"backup": OK_BACKUP, "snapshots": {"stdout": [snapshots_json()]}})
    clock = Clock(T0)
    config = sandbox.config({"docs": SCHEDULED | (job or {})}, scheduler=scheduler)
    engine = await sandbox.engine(config, clock=clock)
    engine._build_entries(clock.now)
    return engine, clock, engine.entries[0]


async def test_due_run_fires_once_and_plans_the_next(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox)
    assert entry.next_fire == T0.replace(hour=2)
    clock.now = T0.replace(hour=2, second=1)
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    assert entry.next_fire == T0.replace(day=2, hour=2)
    result = engine.last_results[("docs", Operation.BACKUP)]
    assert result.trigger == Trigger.SCHEDULE and len(sandbox.journal()) == 1
    labels = {"job": "docs", "operation": "backup"}
    assert (
        metric(engine.metrics, "rustic_scheduler_next_run_timestamp_seconds", labels)
        == entry.next_fire.timestamp()
    )


async def test_late_run_within_grace_is_a_catch_up(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox, misfire_grace="6h")
    clock.now = T0.replace(hour=5)  # e.g. the host was suspended for three hours
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    assert engine.last_results[("docs", Operation.BACKUP)].trigger == Trigger.CATCH_UP


async def test_late_run_beyond_grace_is_missed(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox, misfire_grace="1h")
    clock.now = T0.replace(hour=5)
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    assert sandbox.journal() == []
    assert engine.last_skips[("docs", Operation.BACKUP)].reason == Reason.MISSED
    assert (
        metric(
            engine.metrics,
            "rustic_scheduler_missed_runs_total",
            {"job": "docs", "operation": "backup"},
        )
        == 1
    )


async def test_skip_policy_never_runs_late(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox, {"missed_run_policy": "skip"})
    clock.now = T0.replace(hour=2, minute=5)
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    assert sandbox.journal() == []
    assert engine.last_skips[("docs", Operation.BACKUP)].reason == Reason.MISSED


async def test_several_missed_occurrences_collapse_into_one_run(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox, misfire_grace="24h")
    clock.now = T0.replace(day=4, hour=3)  # four occurrences passed: one run, three missed
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    assert len(sandbox.journal()) == 1
    assert (
        metric(
            engine.metrics,
            "rustic_scheduler_missed_runs_total",
            {"job": "docs", "operation": "backup"},
        )
        == 3
    )
    assert entry.next_fire == T0.replace(day=5, hour=2)


async def test_clock_moving_backwards_does_not_repeat_a_run(sandbox):
    engine, clock, entry = await scheduled_engine(sandbox)
    clock.now = T0.replace(hour=2, second=1)
    engine._fire(entry, clock.now)
    await engine.wait_idle()
    clock.now = T0.replace(hour=1)  # NTP stepped the clock back an hour
    assert entry.next_fire > clock.now + timedelta(hours=20)  # still tomorrow's run
    assert len(sandbox.journal()) == 1


async def test_loop_fires_scheduled_work_and_keeps_a_heartbeat(sandbox):
    sandbox.write_plan({"backup": OK_BACKUP})
    clock = Clock(T0.replace(hour=1, minute=59, second=59))
    engine = await sandbox.engine(sandbox.config({"docs": SCHEDULED}), clock=clock)
    await engine.start()
    await asyncio.sleep(0.3)
    backups = lambda: [e for e in sandbox.journal() if e["command"] == "backup"]  # noqa: E731
    assert engine.ready and backups() == []
    clock.now = T0.replace(hour=2)
    await asyncio.sleep(1.3)  # the loop planned to wake up one second after it started
    await engine.wait_idle()
    assert len(backups()) == 1
    assert engine.last_results[("docs", Operation.BACKUP)].trigger == Trigger.SCHEDULE
    await engine.shutdown()


# ------------------------------------------------- catch-up after downtime


async def start_after_downtime(sandbox, *, state=None, snapshots=None, job=None, now=None):
    plan = {"backup": OK_BACKUP}
    if snapshots is not None:
        plan["snapshots"] = snapshots
    sandbox.write_plan(plan)
    if state is not None:
        sandbox.state_dir.mkdir(exist_ok=True)
        (sandbox.state_dir / "state.json").write_text(json.dumps({"results": state, "running": {}}))
    clock = Clock(now or T0.replace(hour=3))  # the 02:00 run was an hour ago
    engine = await sandbox.engine(sandbox.config({"docs": SCHEDULED | (job or {})}), clock=clock)
    await engine.start()
    await asyncio.sleep(0.4)
    await engine.wait_idle()
    await engine.shutdown()
    return engine


def stored(outcome, started):
    return {
        "docs/backup": {
            "job": "docs",
            "operation": "backup",
            "trigger": "schedule",
            "outcome": outcome,
            "started_at": started.isoformat(),
            "finished_at": started.isoformat(),
        }
    }


async def test_catch_up_when_the_last_recorded_run_predates_the_missed_time(sandbox):
    engine = await start_after_downtime(sandbox, state=stored("succeeded", T0 - timedelta(days=1)))
    assert [e["command"] for e in sandbox.journal()] == ["backup"]
    assert engine.last_results[("docs", Operation.BACKUP)].trigger == Trigger.CATCH_UP


async def test_no_catch_up_when_the_run_already_happened(sandbox):
    await start_after_downtime(sandbox, state=stored("succeeded", T0.replace(hour=2)))
    assert sandbox.journal() == []


async def test_no_catch_up_for_a_run_that_failed_on_time(sandbox):
    await start_after_downtime(sandbox, state=stored("failed", T0.replace(hour=2)))
    assert sandbox.journal() == []


async def test_catch_up_for_a_run_that_was_cut_short(sandbox):
    await start_after_downtime(sandbox, state=stored("interrupted", T0.replace(hour=2)))
    assert [e["command"] for e in sandbox.journal()] == ["backup"]


async def test_without_state_rustic_snapshots_decide(sandbox):
    old = snapshots_json(snapshot("docs", "2026-05-30T02:00:00Z"))
    await start_after_downtime(sandbox, snapshots={"stdout": [old]})
    assert [e["command"] for e in sandbox.journal()] == ["snapshots", "backup"]


async def test_without_state_a_recent_snapshot_means_nothing_was_missed(sandbox):
    recent = snapshots_json(snapshot("docs", "2026-06-01T02:00:05Z"))
    await start_after_downtime(sandbox, snapshots={"stdout": [recent]})
    assert [e["command"] for e in sandbox.journal()] == ["snapshots"]


async def test_catch_up_snapshot_failure_includes_non_terminal_hint(sandbox, caplog):
    await start_after_downtime(
        sandbox, snapshots={"exit": 1, "stderr": ["error: IO error: not a terminal"]}
    )
    assert [e["command"] for e in sandbox.journal()] == ["snapshots"]
    assert "cannot tell whether a run was missed" in caplog.text
    assert "password-file" in caplog.text


@pytest.mark.parametrize("listing", [{"stdout": [snapshots_json()]}, {"exit": 1}])
async def test_without_evidence_nothing_is_caught_up(sandbox, listing):
    await start_after_downtime(sandbox, snapshots=listing)
    assert [e["command"] for e in sandbox.journal()] == ["snapshots"]


async def test_no_catch_up_outside_the_grace_window_or_with_skip_policy(sandbox):
    old = stored("succeeded", T0 - timedelta(days=3))
    await start_after_downtime(sandbox, state=old, job={"misfire_grace": "30m"})
    assert sandbox.journal() == []
    await start_after_downtime(sandbox, state=old, job={"missed_run_policy": "skip"})
    assert sandbox.journal() == []
