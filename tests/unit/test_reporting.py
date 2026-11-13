"""Metrics, notifications, Pushgateway verification, HTTP and MQTT."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web

from helpers import STATUS, SUMMARY, metric
from rustic_scheduler.config import MqttConfig, NotificationsConfig
from rustic_scheduler.httpserver import HttpServer
from rustic_scheduler.metrics import Metrics
from rustic_scheduler.model import (
    Operation,
    OperationResult,
    Outcome,
    Progress,
    Reason,
    Trigger,
    utcnow,
)
from rustic_scheduler.mqtt import MqttPublisher
from rustic_scheduler.notify import Notifier, event_for, render
from rustic_scheduler.pushgateway import PushgatewayClient, parse_groups

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def result(operation=Operation.BACKUP, outcome=Outcome.SUCCEEDED, **kwargs) -> OperationResult:
    defaults = {"started_at": NOW, "finished_at": NOW + timedelta(seconds=65)}
    return OperationResult("docs", operation, Trigger.SCHEDULE, outcome, **(defaults | kwargs))


# ------------------------------------------------------------------ metrics


def test_metric_labels_are_bounded():
    metrics = Metrics()
    metrics.declare("docs", Operation.BACKUP)
    metrics.set_progress("docs", Operation.BACKUP, Progress(NOW, 1, 2, 3, 0.5, 4))
    for operation in Operation:
        done = result(operation, exit_code=0, data={"count": 1, "latest": 1.0, "removed": 1})
        metrics.set_last_result(done)
        metrics.count_result(done)
    allowed = {
        "backup_job",
        "operation",
        "outcome",
        "trigger",
        "reason",
        "result",
        "event",
        "version",
        "rustic_version",
    }
    names = set()
    for family in metrics.registry.collect():
        for sample in family.samples:
            names.add(sample.name)
            assert set(sample.labels) <= allowed, sample
            assert sample.name.startswith("rustic_scheduler_")
            for value in sample.labels.values():
                assert "/" not in value and len(value) < 64
    # Rustic's own pushed metrics use the rustic_backup_ prefix; no collisions.
    assert not any(name.startswith("rustic_backup_") for name in names)


def test_unknown_progress_is_absent_and_known_zero_is_zero():
    metrics = Metrics()
    labels = {"job": "docs", "operation": "backup"}
    metrics.set_progress("docs", Operation.BACKUP, Progress(NOW, bytes_done=0))
    assert metric(metrics, "rustic_scheduler_progress_bytes_done", labels) == 0
    assert metric(metrics, "rustic_scheduler_progress_bytes_total", labels) is None
    metrics.set_progress(
        "docs", Operation.BACKUP, Progress(NOW, bytes_done=5, bytes_total=10, ratio=0.5)
    )
    assert metric(metrics, "rustic_scheduler_progress_ratio", labels) == 0.5
    # A later message without a total must withdraw the old value.
    metrics.set_progress("docs", Operation.BACKUP, Progress(NOW, bytes_done=6))
    assert metric(metrics, "rustic_scheduler_progress_bytes_total", labels) is None


def test_exactly_one_outcome_is_current():
    metrics = Metrics()
    metrics.set_last_result(result(outcome=Outcome.FAILED, reason=Reason.EXIT_CODE, exit_code=1))
    metrics.set_last_result(result(outcome=Outcome.SUCCEEDED, exit_code=0))
    values = {
        outcome: metric(
            metrics,
            "rustic_scheduler_operation_last_outcome",
            {"job": "docs", "operation": "backup", "outcome": outcome},
        )
        for outcome in Outcome
    }
    assert values == {o: (1 if o == Outcome.SUCCEEDED else 0) for o in Outcome}


def test_failure_keeps_the_previous_success_timestamp_and_summary():
    metrics = Metrics()
    labels = {"job": "docs", "operation": "backup"}
    metrics.set_last_result(result(exit_code=0, data={"files_new": 7}))
    success = metric(metrics, "rustic_scheduler_operation_last_success_timestamp_seconds", labels)
    later = NOW + timedelta(days=1)
    metrics.set_last_result(
        result(outcome=Outcome.FAILED, started_at=later, finished_at=later, exit_code=1)
    )
    assert (
        metric(metrics, "rustic_scheduler_operation_last_success_timestamp_seconds", labels)
        == success
    )
    assert (
        metric(metrics, "rustic_scheduler_operation_last_end_timestamp_seconds", labels)
        == later.timestamp()
    )
    assert metric(metrics, "rustic_scheduler_backup_last_files_new", {"job": "docs"}) == 7


def test_exposition_is_valid_prometheus_text():
    metrics = Metrics()
    metrics.info.labels("0.1.0", "0.11.4").set(1)
    text = metrics.render().decode()
    assert 'rustic_scheduler_info{rustic_version="0.11.4",version="0.1.0"} 1.0' in text
    for line in text.splitlines():
        assert line.startswith("#") or re.match(r"^[a-z_]+(\{.*\})? [0-9.e+-]+$", line), line


# ------------------------------------------------------------ notifications


def test_events_and_rendering():
    assert event_for(result()) == "backup_success"
    assert event_for(result(outcome=Outcome.FAILED)) == "backup_failure"
    assert event_for(result(outcome=Outcome.INTERRUPTED)) == "backup_failure"
    assert event_for(result(outcome=Outcome.UNKNOWN)) == "backup_failure"
    assert event_for(result(outcome=Outcome.SKIPPED)) == "backup_skipped"
    assert event_for(result(Operation.CHECK, Outcome.FAILED)) == "maintenance_failure"
    assert event_for(result(Operation.FORGET)) == "maintenance_success"
    title, body = render(
        result(
            outcome=Outcome.FAILED,
            reason=Reason.EXIT_CODE,
            exit_code=1,
            detail="token=abc123 denied",
        )
    )
    assert title == "backup failed: docs"
    assert "Reason: exit_code" in body and "Duration: 1m 05s" in body and "exit status: 1" in body
    assert "abc123" not in body
    _, body = render(
        result(
            data={
                "files_new": 3,
                "files_changed": 1,
                "files_unmodified": 5,
                "data_added_packed": 800,
            }
        )
    )
    assert "3 new, 1 changed, 5 unmodified" in body


class StubTarget:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def __len__(self):
        return 1

    async def async_notify(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.behaviour.pop(0) if len(self.behaviour) > 1 else self.behaviour[0]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(60)
        return outcome


def notifier(tmp_path, behaviour, **config):
    path = tmp_path / "apprise.conf"
    path.write_text("")
    cfg = NotificationsConfig(enabled=True, config_files=[path], retry_delay=0, **config)
    metrics = Metrics()
    instance = Notifier(cfg, metrics)
    target = StubTarget(behaviour)
    instance._load = lambda: target
    return instance, target, metrics


def sent(metrics, event, outcome):
    return metric(
        metrics, "rustic_scheduler_notifications_total", {"event": event, "result": outcome}
    )


async def test_only_selected_events_are_sent(tmp_path):
    instance, target, metrics = notifier(tmp_path, [True], events=["backup_failure"])
    instance.submit(result())
    instance.submit(result(outcome=Outcome.FAILED))
    await instance.drain()
    assert len(target.calls) == 1 and target.calls[0]["title"] == "backup failed: docs"
    assert sent(metrics, "backup_failure", "sent") == 1
    assert sent(metrics, "backup_success", "sent") is None


async def test_default_events_cover_backup_results_and_maintenance_failures(tmp_path):
    instance, target, _ = notifier(tmp_path, [True])
    for item in (
        result(),
        result(outcome=Outcome.FAILED),
        result(Operation.CHECK, Outcome.FAILED),
        result(Operation.FORGET),  # maintenance success: not notified by default
        result(outcome=Outcome.SKIPPED),  # skipped: not notified by default
    ):
        instance.submit(item)
    await instance.drain()
    assert len(target.calls) == 3


async def test_failed_delivery_is_retried_then_counted_never_raised(tmp_path):
    instance, target, metrics = notifier(
        tmp_path, [False, RuntimeError("tgram://secret-token-1234/x"), False], retries=2
    )
    assert await instance.send("backup_success", result()) is False
    assert len(target.calls) == 3
    assert sent(metrics, "backup_success", "failed") == 1
    assert metric(metrics, "rustic_scheduler_notification_last_failure_timestamp_seconds") > 0


async def test_retry_can_succeed(tmp_path):
    instance, target, metrics = notifier(tmp_path, [False, True], retries=2)
    assert await instance.send("backup_success", result()) is True
    assert len(target.calls) == 2 and sent(metrics, "backup_success", "sent") == 1


async def test_hanging_delivery_times_out(tmp_path):
    instance, _, metrics = notifier(tmp_path, ["hang"], retries=0, timeout=0.1)
    assert await instance.send("backup_failure", result(outcome=Outcome.FAILED)) is False
    assert sent(metrics, "backup_failure", "failed") == 1


async def test_notification_failure_never_changes_a_backup_outcome(sandbox, tmp_path):
    sandbox.write_plan({"backup": {"stdout": [STATUS, SUMMARY]}})
    instance, target, metrics = notifier(tmp_path, [RuntimeError("boom")], retries=0)
    engine = await sandbox.engine(notifier=instance)
    instance.metrics = engine.metrics
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await engine.wait_idle()
    await instance.drain()
    assert engine.last_results[("docs", Operation.BACKUP)].outcome == Outcome.SUCCEEDED
    assert sent(engine.metrics, "backup_success", "failed") == 1
    labels = {"job": "docs", "operation": "backup", "outcome": "succeeded"}
    assert metric(engine.metrics, "rustic_scheduler_operation_last_outcome", labels) == 1


async def test_notification_urls_are_registered_for_redaction(tmp_path, caplog):
    path = tmp_path / "apprise.conf"
    path.write_text("json://user:supersecretpw@127.0.0.1:1/hook\n")
    cfg = NotificationsConfig(enabled=True, config_files=[path], retries=0, timeout=3)
    instance = Notifier(cfg, Metrics())
    assert len(instance._load()) == 1
    from rustic_scheduler.redact import REDACTOR

    assert "supersecretpw" not in REDACTOR("failed json://user:supersecretpw@127.0.0.1:1/hook")
    assert await instance.send("backup_success", result()) is False
    assert "supersecretpw" not in caplog.text


# -------------------------------------------------------------- pushgateway


def group(labels, push_time, ok=True, rustic=True):
    item = {
        "labels": labels,
        "last_push_successful": ok,
        "push_time_seconds": {"metrics": [{"value": str(push_time)}]},
    }
    if rustic:
        item["rustic_backup_time"] = {"metrics": [{"value": "1"}]}
    return item


@pytest.fixture
async def gateway():
    state = {"groups": [], "deleted": [], "status": 200}

    async def listing(_):
        if state["status"] != 200:
            return web.Response(status=state["status"])
        return web.json_response({"status": "success", "data": state["groups"]})

    async def delete(request):
        state["deleted"].append(request.path)
        return web.Response(status=202)

    app = web.Application()
    app.router.add_get("/api/v1/metrics", listing)
    app.router.add_delete("/{tail:.*}", delete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield PushgatewayClient(f"http://127.0.0.1:{port}/", 2), state
    await runner.cleanup()


MINE = {"job": "rustic_backup", "snapshot_label": "docs", "hostname": "nas", "paths": "/data"}


async def test_delivery_is_confirmed_by_the_gateway(gateway):
    client, state = gateway
    state["groups"] = [group(MINE, NOW.timestamp() + 5)]
    verification = await client.verify("docs", None, NOW)
    assert verification.result == "delivered" and verification.push_time == NOW.timestamp() + 5


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        ([], "missing"),
        ([group({**MINE, "snapshot_label": "other"}, NOW.timestamp() + 5)], "missing"),
        ([group(MINE, NOW.timestamp() - 3600)], "stale"),
        ([group(MINE, NOW.timestamp() + 5, ok=False)], "stale"),
        ([group(MINE, NOW.timestamp() + 5, rustic=False)], "stale"),
    ],
)
async def test_pushes_that_did_not_arrive_are_not_reported_as_delivered(gateway, groups, expected):
    client, state = gateway
    state["groups"] = groups
    assert (await client.verify("docs", None, NOW)).result == expected


async def test_host_narrows_the_match(gateway):
    client, state = gateway
    state["groups"] = [group({**MINE, "hostname": "laptop"}, NOW.timestamp() + 5)]
    assert (await client.verify("docs", "nas", NOW)).result == "missing"
    assert (await client.verify("docs", "laptop", NOW)).result == "delivered"


async def test_unreachable_or_broken_gateway(gateway):
    client, state = gateway
    state["status"] = 500
    assert (await client.verify("docs", None, NOW)).result == "unreachable"
    dead = PushgatewayClient("http://127.0.0.1:9", 1)
    assert (await dead.verify("docs", None, NOW)).result == "unreachable"


async def test_stale_groups_of_the_same_job_are_deleted_only_on_request(gateway):
    client, state = gateway
    old = {**MINE, "paths": "/old/path"}
    foreign = {**MINE, "snapshot_label": "other", "paths": "/elsewhere"}
    state["groups"] = [
        group(MINE, NOW.timestamp() + 5),
        group(old, NOW.timestamp() - 86400),
        group(foreign, NOW.timestamp() - 86400),
    ]
    assert (await client.verify("docs", None, NOW)).stale_groups_deleted == 0
    assert state["deleted"] == []
    verification = await client.verify("docs", None, NOW, cleanup_stale=True)
    assert verification.stale_groups_deleted == 1
    # base64url("rustic_backup") / ... / base64url("/old/path"); other jobs are untouched
    assert len(state["deleted"]) == 1
    assert state["deleted"][0].startswith("/metrics/job@base64/cnVzdGljX2JhY2t1cA/")
    assert "paths@base64/L29sZC9wYXRo" in state["deleted"][0]


def test_group_parsing_tolerates_missing_fields():
    groups = parse_groups({"data": [{"labels": {"job": "x"}}, {}]})
    assert [g.push_time for g in groups] == [None, None]
    assert parse_groups(None) == [] and parse_groups({}) == []


# --------------------------------------------------------------------- http


@pytest.fixture
async def served(sandbox):
    sandbox.write_plan({"backup": {"stdout": [STATUS, SUMMARY]}})
    engine = await sandbox.engine()
    server = HttpServer(engine, "127.0.0.1", 0, engine.state.control_socket)
    public = web.AppRunner(
        __import__("rustic_scheduler.httpserver", fromlist=["_routes"])._routes(
            engine, control=False
        )
    )
    await public.setup()
    site = web.TCPSite(public, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as session:
        yield engine, session, server
    await public.cleanup()
    if not engine.stop_event.is_set():
        await engine.shutdown()


async def test_a_listening_endpoint_is_not_a_ready_scheduler(served):
    engine, session, _ = served
    # The HTTP server answers, but the scheduling loop has not been started.
    async with session.get("/readyz") as response:
        assert response.status == 503 and (await response.json())["ready"] is False
    async with session.get("/healthz") as response:
        assert response.status == 503
    async with session.get("/metrics") as response:
        assert response.status == 200 and "rustic_scheduler_ready 0.0" in await response.text()

    await engine.start()
    await asyncio.sleep(0.1)
    async with session.get("/readyz") as response:
        assert response.status == 200 and (await response.json())["ready"] is True
    async with session.get("/healthz") as response:
        assert response.status == 200
    async with session.get("/metrics") as response:
        assert "rustic_scheduler_ready 1.0" in await response.text()


async def test_a_stalled_loop_turns_unready(served):
    engine, session, _ = served
    await engine.start()
    await asyncio.sleep(0.1)
    engine.heartbeat = utcnow() - timedelta(minutes=10)  # the loop stopped iterating
    engine._loop_task.cancel()
    async with session.get("/readyz") as response:
        assert response.status == 503
    async with session.get("/healthz") as response:
        assert response.status == 503


async def test_shutdown_turns_unready(served):
    engine, session, _ = served
    await engine.start()
    await engine.shutdown()
    async with session.get("/readyz") as response:
        assert response.status == 503 and (await response.json())["shutting_down"] is True


async def test_network_port_cannot_start_jobs_but_control_socket_can(served, sandbox):
    engine, session, server = served
    async with session.post("/api/v1/jobs/docs/backup/run") as response:
        assert response.status in (404, 405)
    await asyncio.sleep(0.1)
    assert sandbox.journal() == []

    server.port = 0
    await server.start()
    try:
        assert oct(server.socket_path.stat().st_mode & 0o777) == "0o600"
        connector = aiohttp.UnixConnector(path=str(server.socket_path))
        async with aiohttp.ClientSession(connector=connector) as control:
            async with control.post("http://x/api/v1/jobs/docs/forget/run") as response:
                assert (
                    response.status == 409 and "not enabled" in (await response.json())["message"]
                )
            async with control.post("http://x/api/v1/jobs/docs/backup/run") as response:
                assert response.status == 202
            await engine.wait_idle()
            async with control.get("http://x/api/v1/status") as response:
                status = await response.json()
        last = status["jobs"]["docs"]["operations"]["backup"]["last_result"]
        assert last["outcome"] == "succeeded" and last["trigger"] == "manual"
        assert last["log_file"].endswith("-backup.log")
    finally:
        await server.stop()


# --------------------------------------------------------------------- mqtt


async def test_mqtt_publishing_never_blocks_or_raises_without_a_broker():
    metrics = Metrics()
    config = MqttConfig(enabled=True, host="127.0.0.1", port=9, queue_size=2, connect_timeout=1)
    publisher = MqttPublisher(config, metrics)
    task = asyncio.create_task(publisher.run())
    publisher.publish("a/progress", {"x": 1}, droppable=True)  # dropped: not connected
    for index in range(4):  # lifecycle messages wait in the bounded queue
        publisher.publish("a/result", {"n": index})
    await asyncio.sleep(0.3)
    assert metric(metrics, "rustic_scheduler_mqtt_connected") == 0
    assert metric(metrics, "rustic_scheduler_mqtt_messages_total", {"result": "dropped"}) == 3
    assert (
        metric(metrics, "rustic_scheduler_mqtt_messages_total", {"result": "connection_error"}) >= 1
    )
    await publisher.stop(task, timeout=1)
    assert task.done()


async def test_backup_succeeds_with_mqtt_enabled_but_broker_down(sandbox):
    sandbox.write_plan({"backup": {"stdout": [STATUS, SUMMARY]}})
    metrics_holder = Metrics()
    publisher = MqttPublisher(
        MqttConfig(enabled=True, host="127.0.0.1", port=9, connect_timeout=1), metrics_holder
    )
    task = asyncio.create_task(publisher.run())
    engine = await sandbox.engine(
        sandbox.config(mqtt={"enabled": True, "host": "127.0.0.1", "port": 9}), mqtt=publisher
    )
    engine.submit("docs", Operation.BACKUP, Trigger.MANUAL)
    await engine.wait_idle()
    assert engine.last_results[("docs", Operation.BACKUP)].outcome == Outcome.SUCCEEDED
    await publisher.stop(task, timeout=1)


def test_mqtt_topics_and_redaction():
    publisher = MqttPublisher(MqttConfig(enabled=True, topic_prefix="home/backup/"), Metrics())
    assert (
        publisher.topic("jobs", "docs", "backup", "result") == "home/backup/jobs/docs/backup/result"
    )
    publisher.connected = True
    publisher.publish("t", {"detail": "password=hunter2"})
    assert "hunter2" not in publisher._queue.get_nowait().payload
