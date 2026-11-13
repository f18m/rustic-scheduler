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

    for path in ("/", "/app.js", "/api/v1/overview"):
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


# ------------------------------------------------------------ configuration


def test_web_configuration(sandbox):
    assert sandbox.config().web.enabled is False
    config = sandbox.config(web={"enabled": True})
    assert config.web.port == 9301 and config.web.operations == ["backup", "check"]
    assert config.web.password_file is None and config.web.history_size == 50

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


# ------------------------------------------- description and the latest snapshot


async def test_overview_shows_the_description_and_the_latest_snapshot(site, sandbox):
    listing = snapshots_json(
        snapshot("docs", "2026-01-01T10:00:00+01:00", "a"),
        snapshot("docs", "2026-01-03T10:00:00+01:00", "b"),
    )
    engine, session = await site(
        job={"description": "Family <b>documents</b>", "snapshots": {"enabled": True}},
        plan={"snapshots": [{"stdout": [listing]}, {"exit": 1, "stderr": ["error: boom"]}]},
    )
    job = (await overview(session))["jobs"][0]
    assert job["description"] == "Family <b>documents</b>"  # the page inserts it as text
    assert job["snapshots_enabled"] and job["snapshots"] is None  # nothing collected yet

    engine.trigger("docs", Operation.SNAPSHOTS)
    await engine.wait_idle()
    job = (await overview(session))["jobs"][0]
    assert job["snapshots"] == {
        "count": 2,
        "latest_at": "2026-01-03T09:00:00+00:00",
        "latest_id": "b" * 8,
        "latest_bytes": 4242,
        "latest_files": 17,
        "collected_at": engine.last_results[("docs", Operation.SNAPSHOTS)].finished_at.isoformat(),
    }

    # A failed collection keeps what is known; `collected_at` shows its age.
    engine.trigger("docs", Operation.SNAPSHOTS)
    await engine.wait_idle()
    assert engine.last_results[("docs", Operation.SNAPSHOTS)].outcome == Outcome.FAILED
    assert (await overview(session))["jobs"][0]["snapshots"] == job["snapshots"]


async def test_overview_without_description_or_telemetry(site):
    _, session = await site()
    job = (await overview(session))["jobs"][0]
    assert job["description"] is None
    assert job["snapshots"] is None and not job["snapshots_enabled"]


async def test_page_script_shows_description_and_snapshot_totals_as_text(site):
    _, session = await site()
    async with session.get("/app.js") as response:
        script = await response.text()
    assert "job.description" in script and "latest_files" in script and "latest_bytes" in script
    assert "innerHTML" not in script
