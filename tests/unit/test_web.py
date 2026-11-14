"""The optional web interface: page, overview, run requests, protection, history."""

import asyncio
import base64
import socket

import aiohttp
import pytest
from aiohttp import web

from helpers import STATUS, SUMMARY, forget_json, metric, snapshot, snapshots_json
from rustic_scheduler.config import ConfigError
from rustic_scheduler.model import Operation, Outcome
from rustic_scheduler.web import CSRF_HEADER, CSRF_VALUE, WebServer, create_app

SENT = {CSRF_HEADER: CSRF_VALUE}
JOB = {"profiles": ["repo"], "sources": ["/data"]}


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
async def site(sandbox):
    """Returns a function that serves the interface for a configuration."""
    cleanup = []

    async def start(job=None, plan=None, **settings):
        sandbox.write_plan(plan or {"backup": {"stdout": [STATUS, SUMMARY]}})
        config = sandbox.config(
            {"docs": JOB | (job or {})},
            web={"enabled": True} | settings,
        )
        engine = await sandbox.engine(config)
        runner = web.AppRunner(create_app(engine, config.web, "0.11.4"))
        await runner.setup()
        tcp = web.TCPSite(runner, "127.0.0.1", 0)
        await tcp.start()
        port = tcp._server.sockets[0].getsockname()[1]
        session = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}")
        cleanup.append((engine, runner, session))
        return engine, session

    yield start
    for engine, runner, session in cleanup:
        await session.close()
        await runner.cleanup()
        if not engine.stop_event.is_set():
            await engine.shutdown()


def commands(engine, result) -> float:
    return metric(engine.metrics, "rustic_scheduler_web_commands_total", {"result": result})


async def overview(session) -> dict:
    async with session.get("/api/v1/overview") as response:
        assert response.status == 200
        return await response.json()


def row(data, operation, job="docs") -> dict:
    found = next(j for j in data["jobs"] if j["name"] == job)
    return next(o for o in found["operations"] if o["operation"] == operation)


# --------------------------------------------------------------------- page


async def test_page_and_assets_are_served_with_a_strict_policy(site):
    _, session = await site()
    for path, kind in (
        ("/", "text/html"),
        ("/app.js", "text/javascript"),
        ("/app.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ):
        async with session.get(path) as response:
            assert response.status == 200 and response.content_type == kind
            policy = response.headers["Content-Security-Policy"]
            assert "default-src 'none'" in policy and "unsafe" not in policy
            assert response.headers["X-Frame-Options"] == "DENY"
            text = await response.text()
    async with session.get("/") as response:
        text = await response.text()
    # Nothing inline and nothing from another site, or the policy would block it.
    assert "<style" not in text and "style=" not in text and "http" not in text
    assert text.count("<script") == 1 and '<script src="app.js">' in text
    assert "Using Rustic directly" in text and "has not tested a Glacier" in text
    assert '<link rel="icon" type="image/svg+xml" href="favicon.svg">' in text
    # One tab per section, each with its panel.
    for tab in ("jobs", "snapshots", "history", "manual"):
        assert f'id="tab-{tab}" aria-controls="panel-{tab}"' in text
        assert f'id="panel-{tab}" role="tabpanel"' in text


async def test_the_page_script_never_builds_html_from_data(site):
    _, session = await site()
    async with session.get("/app.js") as response:
        script = await response.text()
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "eval(" not in script and "document.write" not in script


# ----------------------------------------------------------------- overview


async def test_overview_lists_operations_and_what_may_be_started(site):
    _, session = await site(
        job={"check": {"enabled": True}, "forget": {"enabled": True, "keep": {"keep-last": 1}}}
    )
    data = await overview(session)
    assert data["rustic_version"] == "0.11.4" and data["history"] == []
    names = [o["operation"] for o in data["jobs"][0]["operations"]]
    assert names == ["backup", "forget", "check"]  # snapshots and prune are not enabled
    assert row(data, "backup")["can_run"] and row(data, "check")["can_run"]
    forget = row(data, "forget")
    assert forget["can_run"] is False and forget["destructive"] is True  # not in web.operations
    assert row(data, "check")["state"] == "idle"


async def test_a_running_backup_shows_progress_then_lands_in_history(site):
    engine, session = await site(
        plan={"backup": {"stdout": [STATUS, SUMMARY], "line_delay": 0.6}}, history_size=3
    )
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 202 and (await response.json())["accepted"] is True

    for _ in range(100):
        backup = row(await overview(session), "backup")
        if backup["progress"]:
            break
        await asyncio.sleep(0.05)
    assert backup["state"] == "running" and backup["trigger"] == "web"
    assert backup["started_at"] is not None
    assert backup["progress"]["ratio"] == pytest.approx(0.2)
    assert backup["progress"]["bytes_done"] == 1000 and backup["progress"]["bytes_total"] == 5000

    await engine.wait_idle()
    data = await overview(session)
    backup = row(data, "backup")
    assert backup["state"] == "idle" and backup["progress"] is None
    assert backup["last_result"]["outcome"] == "succeeded"
    assert [(h["operation"], h["outcome"], h["trigger"]) for h in data["history"]] == [
        ("backup", "succeeded", "web")
    ]
    assert commands(engine, "accepted") == 1 and commands(engine, "rejected") == 0


async def test_history_is_newest_first_bounded_and_includes_skips(site):
    engine, session = await site(history_size=3)
    for _ in range(4):
        engine.trigger("docs", Operation.BACKUP)
        engine.trigger("docs", Operation.BACKUP)  # refused: recorded as a skipped run
        await engine.wait_idle()
    history = (await overview(session))["history"]
    assert len(history) == 3
    assert [h["outcome"] for h in history] == ["succeeded", "skipped", "succeeded"]
    assert history[1]["reason"] == "already_running"
    finished = [h["finished_at"] for h in history]
    assert finished == sorted(finished, reverse=True)


async def test_history_after_a_restart_holds_the_last_known_results(site, sandbox):
    engine, _ = await site()
    engine.trigger("docs", Operation.BACKUP)
    await engine.wait_idle()
    await engine.shutdown()

    again = await sandbox.engine(engine.config)
    await again.start()
    try:
        assert [(r.operation, r.outcome) for r in again.history] == [
            (Operation.BACKUP, Outcome.SUCCEEDED)
        ]
    finally:
        await again.shutdown()


# --------------------------------------------------------------------- runs


async def test_forget_runs_only_when_listed_and_enabled(site, sandbox):
    old, new = (
        snapshot("docs", "2026-01-01T00:00:00Z", "1"),
        snapshot("docs", "2026-01-02T00:00:00Z"),
    )
    plan = {"forget": {"stdout": [forget_json((old, False), (new, True))]}}
    forgetting = {"forget": {"enabled": True, "keep": {"keep-last": 1}}}

    engine, session = await site(job=forgetting, plan=plan)  # default: backup and check only
    async with session.post("/api/v1/jobs/docs/forget/run", headers=SENT) as response:
        assert response.status == 409
        assert "not listed in web.operations" in (await response.json())["message"]
    assert sandbox.journal() == [] and commands(engine, "rejected") == 1

    engine, session = await site(plan=plan, operations=["forget"])  # listed, not enabled
    async with session.post("/api/v1/jobs/docs/forget/run", headers=SENT) as response:
        assert response.status == 409 and "not enabled" in (await response.json())["message"]
    assert sandbox.journal() == []

    engine, session = await site(job=forgetting, plan=plan, operations=["forget"])
    async with session.post("/api/v1/jobs/docs/forget/run", headers=SENT) as response:
        assert response.status == 202
    await engine.wait_idle()
    result = engine.last_results[("docs", Operation.FORGET)]
    assert result.outcome == Outcome.SUCCEEDED and result.trigger == "web"
    # The preview still comes first.
    assert [("--dry-run" in e["argv"]) for e in sandbox.journal() if e["command"] == "forget"] == [
        True,
        False,
    ]


async def test_a_second_request_is_refused_without_recording_a_skip(site):
    engine, session = await site(plan={"backup": {"stdout": [STATUS, SUMMARY], "line_delay": 0.3}})
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 202
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 409 and "already" in (await response.json())["message"]
    await engine.wait_idle()
    assert ("docs", Operation.BACKUP) not in engine.last_skips
    assert [r.outcome for r in engine.history] == [Outcome.SUCCEEDED]


async def test_unknown_job_or_operation_starts_nothing(site, sandbox):
    engine, session = await site()
    for path in ("/api/v1/jobs/other/backup/run", "/api/v1/jobs/docs/restore/run"):
        async with session.post(path, headers=SENT) as response:
            assert response.status == 404
    assert sandbox.journal() == [] and commands(engine, "rejected") == 2


async def test_an_empty_operation_list_makes_the_page_read_only(site, sandbox):
    _, session = await site(operations=[])
    assert row(await overview(session), "backup")["can_run"] is False
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 409
    assert sandbox.journal() == []


# --------------------------------------------------------------- protection


@pytest.mark.parametrize(
    "headers",
    [
        {},  # a plain cross-site form post
        {CSRF_HEADER: "XMLHttpRequest"},
        SENT | {"Origin": "http://evil.example"},
    ],
)
async def test_requests_from_another_site_cannot_start_a_run(site, sandbox, headers):
    engine, session = await site()
    async with session.post("/api/v1/jobs/docs/backup/run", headers=headers) as response:
        assert response.status == 403
    await asyncio.sleep(0.1)
    assert sandbox.journal() == [] and engine.history == type(engine.history)(maxlen=50)


async def test_same_origin_requests_are_accepted(site):
    engine, session = await site()
    origin = str(session._base_url).rstrip("/")
    async with session.post(
        "/api/v1/jobs/docs/backup/run", headers=SENT | {"Origin": origin}
    ) as response:
        assert response.status == 202
    await engine.wait_idle()


async def test_get_cannot_start_a_run(site, sandbox):
    _, session = await site()
    async with session.get("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 405
    assert sandbox.journal() == []


async def test_password_protects_every_route(site, sandbox):
    secret = sandbox.root / "web_password"
    secret.write_text("correct horse\n")
    engine, session = await site(password_file=str(secret), username="me")

    for path in ("/", "/app.js", "/favicon.svg", "/api/v1/overview", "/api/v1/jobs/docs/snapshots"):
        async with session.get(path) as response:
            assert response.status == 401 and "Basic" in response.headers["WWW-Authenticate"]
    for wrong in (
        basic("me", "wrong"),
        basic("admin", "correct horse"),
        {"Authorization": "Basic !"},
    ):
        async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT | wrong) as response:
            assert response.status == 401
    assert sandbox.journal() == []

    good = basic("me", "correct horse")
    async with session.get("/api/v1/overview", headers=good) as response:
        assert response.status == 200 and "correct horse" not in await response.text()
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT | good) as response:
        assert response.status == 202
    await engine.wait_idle()

    secret.write_text("rotated")  # picked up without a restart
    async with session.get("/", headers=good) as response:
        assert response.status == 401
    async with session.get("/", headers=basic("me", "rotated")) as response:
        assert response.status == 200


async def test_an_unreadable_password_file_never_means_no_password(site, sandbox):
    secret = sandbox.root / "web_password"
    secret.write_text("pw-12345")
    _, session = await site(password_file=str(secret))
    secret.unlink()
    async with session.get("/api/v1/overview", headers=basic("admin", "pw-12345")) as response:
        assert response.status == 503
    async with session.post("/api/v1/jobs/docs/backup/run", headers=SENT) as response:
        assert response.status == 503
    assert sandbox.journal() == []


# ------------------------------------------------------- manual instructions


async def test_manual_instructions_match_the_job_and_hide_secret_values(site, sandbox):
    _, session = await site(
        job={
            "label": "my docs",
            "host": "nas",
            "env": {
                "AWS_SHARED_CREDENTIALS_FILE": "/run/secrets/aws",
                "RUSTIC_PASSWORD": "hunter2-xyz",
            },
        },
    )
    data = await overview(session)
    manual = data["jobs"][0]["manual"]
    assert manual["profiles"] == f"-P {sandbox.profiles}/repo.toml"
    assert manual["scope"] == "--filter-label 'my docs' --filter-host nas"
    assert manual["helper"] == "rustic-scheduler rustic docs --"
    assert "AWS_SHARED_CREDENTIALS_FILE=/run/secrets/aws" in manual["env"]
    assert "hunter2-xyz" not in str(data) and "FAKE_RUSTIC" not in str(manual["helper"])


# ------------------------------------------------- snapshots and their files

OLD, NEW, FOREIGN = "a" * 64, "b" * 64, "c" * 64
SNAPSHOTS = snapshots_json(
    snapshot("docs", "2026-01-01T03:00:00+00:00", "a"),
    snapshot("docs", "2026-01-02T03:00:00+00:00", "b") | {"summary": {}, "tags": ["weekly"]},
    snapshot("someone-else", "2026-01-03T03:00:00+00:00", "c"),
)
TREE = {
    "nodes": [
        {
            "name": "notes <b>.txt",
            "type": "file",
            "mode": 0o640,
            "mtime": "2026-01-01T02:00:00.123456789+01:00",
            "ctime": "2026-01-01T02:00:01+01:00",
            "uid": 1000,
            "gid": 100,
            "user": "me",
            "group": "users",
            "size": 12,
            "content": ["d" * 64],
        },
        {"name": "link", "type": "symlink", "linktarget": "notes", "mode": 0o1000000755, "size": 5},
        # What Rustic prints for a parent of the backed-up path: no times, no owner.
        {"name": "archive", "type": "dir", "mode": 0o755, "content": None, "subtree": "e" * 64},
    ]
}
BROWSE = {
    "backup": {"stdout": [STATUS, SUMMARY]},
    "snapshots": {"stdout": SNAPSHOTS},
    "cat": {"stdout": [TREE]},
}


async def fetch(session, path, status=200, headers=SENT) -> dict:
    async with session.get(path, headers=headers) as response:
        assert response.status == status, await response.text()
        return await response.json()


async def test_browsing_is_off_unless_enabled(site, sandbox):
    _, session = await site(plan=BROWSE)
    assert (await overview(session))["browse"] is False
    for path in ("/api/v1/jobs/docs/snapshots", f"/api/v1/jobs/docs/snapshots/{OLD}/files"):
        assert "web.browse.enabled" in (await fetch(session, path, 404))["message"]
    assert sandbox.journal() == []


async def test_snapshots_of_a_job_are_listed_newest_first(site, sandbox):
    _, session = await site(plan=BROWSE, browse={"enabled": True})
    assert (await overview(session))["browse"] is True
    data = await fetch(session, "/api/v1/jobs/docs/snapshots")
    # Only the job's own snapshots, also when Rustic returns others.
    assert [s["id"] for s in data["snapshots"]] == [NEW, OLD]
    assert data["stale"] is False and data["job"] == "docs"
    old = data["snapshots"][1]
    assert old["time"] == "2026-01-01T03:00:00+00:00" and old["paths"] == ["/data"]
    assert old["bytes"] == 4242 and old["hostname"] == "h"
    # What Rustic did not report is null, not zero.
    assert data["snapshots"][0] | {"id": 0, "time": 0} == {
        "id": 0,
        "time": 0,
        "hostname": "h",
        "paths": ["/data"],
        "tags": ["weekly"],
        "files": None,
        "bytes": None,
        "bytes_added": None,
    }
    (entry,) = sandbox.journal()
    assert entry["argv"][-4:] == ["snapshots", "--json", "--filter-label", "docs"]
    assert (await fetch(session, "/api/v1/jobs/nope/snapshots", 404))["message"] == "unknown job"


async def test_the_snapshot_list_is_kept_until_something_ran_or_refresh(site, sandbox):
    engine, session = await site(plan=BROWSE, browse={"enabled": True})
    first = await fetch(session, "/api/v1/jobs/docs/snapshots")
    assert await fetch(session, "/api/v1/jobs/docs/snapshots") == first
    assert sandbox.commands() == ["snapshots"]
    await fetch(session, "/api/v1/jobs/docs/snapshots?refresh=1")
    assert sandbox.commands() == ["snapshots", "snapshots"]
    # A finished operation may have added or removed snapshots.
    engine.trigger("docs", Operation.BACKUP)
    await engine.wait_idle()
    await fetch(session, "/api/v1/jobs/docs/snapshots")
    assert sandbox.commands().count("snapshots") == 3
    # Looking at snapshots is not an operation: no result, no history, no state.
    assert [r.operation for r in engine.history] == [Operation.BACKUP]
    assert (engine.runtimes["docs"].name, Operation.SNAPSHOTS) not in engine.last_results


async def test_files_of_a_snapshot_one_directory_at_a_time(site, sandbox):
    _, session = await site(plan=BROWSE, browse={"enabled": True})
    url = f"/api/v1/jobs/docs/snapshots/{OLD}/files"
    data = await fetch(session, f"{url}?path=/data//sub%20dir/")
    assert data["path"] == "/data/sub dir" and data["total"] == 3 and not data["truncated"]
    assert [e["name"] for e in data["entries"]] == ["archive", "link", "notes <b>.txt"]
    folder, link, notes = data["entries"]
    assert notes == {
        "name": "notes <b>.txt",
        "type": "file",
        "size": 12,
        "modified": "2026-01-01T02:00:00.123456789+01:00",
        "changed": "2026-01-01T02:00:01+01:00",
        "permissions": 0o640,
        "user": "me",
        "group": "users",
        "uid": 1000,
        "gid": 100,
        "link_target": None,
    }
    assert link["link_target"] == "notes" and link["permissions"] == 0o755
    assert link["size"] is None  # not a content size
    assert folder["type"] == "dir" and folder["modified"] is None and folder["uid"] is None
    # One argument, in the job's repository, after the scope was checked with `snapshots`.
    assert sandbox.commands() == ["snapshots", "cat"]
    argv = sandbox.journal()[-1]["argv"]
    assert argv[-3:] == ["cat", "tree", f"{OLD}:/data/sub dir"]
    assert argv[:2] == ["-P", f"{sandbox.profiles}/repo.toml"]

    # A snapshot does not change: the same directory is not asked for twice.
    assert await fetch(session, f"{url}?path=/data/sub%20dir") == data
    assert sandbox.commands() == ["snapshots", "cat"]
    assert (await fetch(session, url))["path"] == "/"
    assert sandbox.journal()[-1]["argv"][-1] == f"{OLD}:/"


async def test_a_large_directory_is_truncated(site):
    _, session = await site(plan=BROWSE, browse={"enabled": True, "max_entries": 2})
    data = await fetch(session, f"/api/v1/jobs/docs/snapshots/{OLD}/files")
    assert data["total"] == 3 and data["truncated"] is True
    assert [e["name"] for e in data["entries"]] == ["archive", "link"]


@pytest.mark.parametrize(
    ("target", "status", "message"),
    [
        (f"{FOREIGN}/files", 404, "not one of this job's snapshots"),  # another job's
        (f"{'f' * 64}/files", 404, "not one of this job's snapshots"),
        ("latest/files", 400, "full id"),
        (f"{OLD[:8]}/files", 400, "full id"),
        (f"{OLD}/files?path=/data/../etc", 400, "must not contain"),
        (f"{OLD}/files?path=/data/%00", 400, "invalid path"),
    ],
)
async def test_only_the_jobs_own_snapshots_and_plain_paths(site, sandbox, target, status, message):
    _, session = await site(plan=BROWSE, browse={"enabled": True})
    data = await fetch(session, f"/api/v1/jobs/docs/snapshots/{target}", status)
    assert message in data["message"]
    assert "cat" not in sandbox.commands()


@pytest.mark.parametrize(
    "headers", [{}, {CSRF_HEADER: "other"}, SENT | {"Origin": "http://evil.example"}]
)
async def test_another_site_cannot_make_the_scheduler_run_rustic(site, sandbox, headers):
    _, session = await site(plan=BROWSE, browse={"enabled": True})
    for path in ("/api/v1/jobs/docs/snapshots", f"/api/v1/jobs/docs/snapshots/{OLD}/files"):
        await fetch(session, path, 403, headers=headers)
    assert sandbox.journal() == []


async def test_a_busy_repository_is_not_queued_behind(site, sandbox, monkeypatch):
    monkeypatch.setattr("rustic_scheduler.browse.LOCK_WAIT", 0.05)
    engine, session = await site(plan=BROWSE, browse={"enabled": True})
    files = f"/api/v1/jobs/docs/snapshots/{OLD}/files"
    lock = engine.runtimes["docs"].lock_key
    async with engine.locks.hold(lock, 1):
        for path in ("/api/v1/jobs/docs/snapshots", files):
            assert "in use" in (await fetch(session, path, 409))["message"]
    assert sandbox.journal() == []

    listed = await fetch(session, "/api/v1/jobs/docs/snapshots")
    seen = await fetch(session, files)
    async with engine.locks.hold(lock, 1):
        # What is already known is still shown, and says that it may be out of date.
        stale = await fetch(session, "/api/v1/jobs/docs/snapshots?refresh=1")
        assert stale == listed | {"stale": True}
        assert await fetch(session, files) == seen
        assert "in use" in (await fetch(session, f"{files}?path=/data", 409))["message"]
    assert sandbox.commands() == ["snapshots", "cat"]


async def test_rustic_errors_are_reported_and_secrets_masked(site, sandbox):
    sandbox.write_plan({})
    _, session = await site(
        browse={"enabled": True},
        plan=BROWSE
        | {
            "cat": [
                # The shape of Rustic 0.11.4's error report.
                {
                    "exit": 1,
                    "stderr": [
                        "error: `rustic_core` experienced an error related to `internal "
                        "operations`.",
                        "",
                        "Message:",
                        "Node `nope` not found in tree.",
                        "",
                        "Some additional details ...",
                    ],
                },
                {"exit": 1, "stderr": ["error: backend refused password=hunter2-xyz"]},
                {"stdout": "not json"},
                {"stdout": [{"nodes": [{"type": "file"}]}]},
            ]
        },
    )
    url = f"/api/v1/jobs/docs/snapshots/{OLD}/files?path=/"
    missing = await fetch(session, f"{url}a", 404)
    assert missing["message"] == "Node `nope` not found in tree."
    failed = (await fetch(session, f"{url}b", 502))["message"]
    assert "backend refused" in failed and "hunter2-xyz" not in failed
    for name in ("c", "d"):
        data = await fetch(session, f"{url}{name}", 502)
        assert data["message"] == "unreadable output from `rustic cat tree`"
    # A failure is not remembered as a listing.
    assert sandbox.commands().count("cat") == 4

    sandbox.write_plan({"snapshots": {"exit": 1, "stderr": ["error: no repository"]}})
    data = await fetch(session, "/api/v1/jobs/docs/snapshots?refresh=1", 502)
    assert "no repository" in data["message"]


async def test_nothing_is_listed_during_shutdown(site, sandbox):
    engine, session = await site(plan=BROWSE, browse={"enabled": True})
    await engine.shutdown()
    assert "shutting down" in (await fetch(session, "/api/v1/jobs/docs/snapshots", 503))["message"]
    assert sandbox.journal() == []


# ------------------------------------------------------------ configuration


def test_web_configuration(sandbox):
    assert sandbox.config().web.enabled is False
    config = sandbox.config(web={"enabled": True})
    assert config.web.port == 9301 and config.web.operations == ["backup", "check"]
    assert config.web.password_file is None and config.web.history_size == 50
    assert config.web.browse.enabled is False and config.web.browse.max_entries == 5000
    browse = sandbox.config(web={"enabled": True, "browse": {"enabled": True, "timeout": "5m"}})
    assert browse.web.browse.enabled and browse.web.browse.timeout.total_seconds() == 300

    # check with nothing to start it is fine once the page may start it ...
    sandbox.config({"docs": JOB | {"check": {"enabled": True}}}, web={"enabled": True})
    # ... but not when the web interface is off or does not list it.
    for section in ({"enabled": False}, {"enabled": True, "operations": ["backup"]}):
        with pytest.raises(ConfigError) as info:
            sandbox.config({"docs": JOB | {"check": {"enabled": True}}}, web=section)
        assert "web.operations" in info.value.problems[0]


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        ({"enabled": True, "port": 9300}, "web.port: the web interface needs its own port"),
        ({"enabled": True, "password_file": "/nonexistent/pw"}, "web.password_file: file"),
        ({"enabled": True, "operations": ["restore"]}, "web.operations.0"),
        ({"enabled": True, "username": "a:b"}, "must not contain ':'"),
        ({"enabled": True, "history_size": 0}, "web.history_size"),
        ({"enabled": True, "password": "inline"}, "web.password: unknown setting"),
        ({"enabled": True, "browse": {"max_entries": 0}}, "web.browse.max_entries"),
        ({"enabled": True, "browse": {"timeout": "0s"}}, "web.browse"),
        ({"enabled": True, "browse": {"paths": ["/"]}}, "web.browse.paths: unknown setting"),
        ({"enabled": True, "browse": True}, "web.browse"),
    ],
)
def test_invalid_web_configuration(sandbox, section, problem):
    with pytest.raises(ConfigError) as info:
        sandbox.config(web=section)
    assert any(problem in text for text in info.value.problems), info.value.problems


async def test_web_server_listens_and_stops(sandbox):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = sandbox.config(web={"enabled": True, "host": "127.0.0.1", "port": port})
    engine = await sandbox.engine(config)
    server = WebServer(engine, config.web, "0.11.4")
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/v1/overview") as response:
                assert response.status == 200
            # The read-only metrics routes are not duplicated here.
            async with session.get(f"http://127.0.0.1:{port}/metrics") as response:
                assert response.status == 404
    finally:
        await server.stop()
