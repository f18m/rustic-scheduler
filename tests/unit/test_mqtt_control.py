"""MQTT commands, the retained state topic and Home Assistant discovery."""

import asyncio
import json

import pytest

from helpers import STATUS, SUMMARY, forget_json, metric, snapshot
from rustic_scheduler.app import connect_mqtt_features
from rustic_scheduler.config import ConfigError, MqttConfig, WebConfig, parse_config
from rustic_scheduler.homeassistant import discovery_messages
from rustic_scheduler.metrics import Metrics
from rustic_scheduler.model import Operation, Outcome
from rustic_scheduler.mqtt import MqttPublisher

RUN = "rustic-scheduler/jobs/docs/{}/run"


async def controlled(sandbox, job=None, operations=None, **mqtt):
    """An engine with MQTT commands enabled and a publisher that only queues."""
    commands = {"enabled": True} | ({"operations": operations} if operations else {})
    config = sandbox.config(
        {"docs": {"profiles": ["repo"], "sources": ["/data"]} | (job or {})},
        mqtt={"enabled": True, "commands": commands} | mqtt,
    )
    publisher = MqttPublisher(config.mqtt, Metrics())
    publisher.connected = True
    engine = await sandbox.engine(config, mqtt=publisher)
    connect_mqtt_features(config, engine, publisher, engine.metrics)
    return engine, publisher


def sent(publisher) -> list[tuple[str, str, bool]]:
    messages = []
    while not publisher._queue.empty():
        message = publisher._queue.get_nowait()
        messages.append((message.topic, message.payload, message.retain))
    return messages


def events(messages, operation="backup") -> list[dict]:
    topic = f"rustic-scheduler/jobs/docs/{operation}/event"
    return [json.loads(payload) for t, payload, _ in messages if t == topic]


def commands(engine, result) -> float:
    return metric(engine.metrics, "rustic_scheduler_mqtt_commands_total", {"result": result})


# ----------------------------------------------------------------- commands


async def test_a_run_message_starts_a_backup_and_reports_its_state(sandbox):
    sandbox.write_plan({"backup": {"stdout": [STATUS, SUMMARY]}})
    engine, publisher = await controlled(sandbox)
    assert publisher.subscriptions == ["rustic-scheduler/jobs/+/+/run"]

    publisher.on_message(RUN.format("backup"), b" RUN\n", False)
    await engine.wait_idle()

    result = engine.last_results[("docs", Operation.BACKUP)]
    assert result.outcome == Outcome.SUCCEEDED and result.trigger == "mqtt"
    assert commands(engine, "accepted") == 1 and commands(engine, "rejected") == 0
    messages = sent(publisher)
    assert [e["event"] for e in events(messages)] == ["queued", "started", "finished"]
    states = [(p, r) for t, p, r in messages if t == "rustic-scheduler/jobs/docs/backup/state"]
    assert states == [("queued", True), ("running", True), ("idle", True)]


@pytest.mark.parametrize(
    ("topic", "payload", "retained"),
    [
        (RUN.format("backup"), b"run", True),  # left on the broker
        (RUN.format("backup"), b"", False),
        (RUN.format("backup"), b"ON", False),
        (RUN.format("restore"), b"run", False),
        ("rustic-scheduler/jobs/other/backup/run", b"run", False),
    ],
)
async def test_invalid_messages_start_nothing(sandbox, topic, payload, retained):
    engine, publisher = await controlled(sandbox)
    publisher.on_message(topic, payload, retained)
    await engine.wait_idle()
    assert sandbox.journal() == [] and commands(engine, "invalid") == 1
    assert sent(publisher) == []


async def test_other_topics_are_not_commands(sandbox):
    engine, publisher = await controlled(sandbox)
    for topic in ("rustic-scheduler/jobs/docs/backup/result", "x/jobs/docs/backup/run"):
        publisher.on_message(topic, b"run", False)
    assert sandbox.journal() == [] and commands(engine, "invalid") == 0


async def test_forget_must_be_listed_and_enabled_on_the_job(sandbox):
    forget = {"forget": {"enabled": True, "keep": {"keep-last": 1}}}

    # Enabled on the job, but not among the operations MQTT may start.
    engine, publisher = await controlled(sandbox, forget)
    publisher.on_message(RUN.format("forget"), b"run", False)
    await engine.wait_idle()
    assert sandbox.journal() == [] and commands(engine, "rejected") == 1
    (event,) = events(sent(publisher), "forget")
    assert event["event"] == "rejected" and "mqtt.commands.operations" in event["detail"]

    # Listed, but the job has no forget: the scheduler's own rule still holds.
    engine, publisher = await controlled(sandbox, operations=["backup", "forget"])
    publisher.on_message(RUN.format("forget"), b"run", False)
    await engine.wait_idle()
    assert sandbox.journal() == [] and commands(engine, "rejected") == 1
    assert "not enabled" in events(sent(publisher), "forget")[0]["detail"]


async def test_a_listed_and_enabled_forget_runs_with_its_safeguards(sandbox):
    kept, removed = snapshot("docs", "2026-05-02T00:00:00Z"), snapshot("docs", "2026-05-01T00:00:00Z", "c")  # fmt: skip
    sandbox.write_plan({"forget": {"stdout": [forget_json((kept, True), (removed, False))]}})
    engine, publisher = await controlled(
        sandbox,
        {"forget": {"enabled": True, "keep": {"keep-last": 1}}},
        operations=["forget"],
    )
    publisher.on_message(RUN.format("forget"), b"run", False)
    await engine.wait_idle()
    result = engine.last_results[("docs", Operation.FORGET)]
    assert result.outcome == Outcome.SUCCEEDED and result.trigger == "mqtt"
    runs = [e["argv"] for e in sandbox.journal() if e["command"] == "forget"]
    assert "--dry-run" in runs[0] and "--dry-run" not in runs[1]  # previewed first

    # backup was left out of the list, so it cannot be started.
    publisher.on_message(RUN.format("backup"), b"run", False)
    assert commands(engine, "rejected") == 1


async def test_a_repeated_message_is_refused_without_recording_a_skip(sandbox):
    sandbox.write_plan({"backup": {"stdout": [STATUS, SUMMARY], "sleep": 0.5}})
    engine, publisher = await controlled(sandbox)
    publisher.on_message(RUN.format("backup"), b"run", False)
    await asyncio.sleep(0.1)
    publisher.on_message(RUN.format("backup"), b"run", False)
    await engine.wait_idle()
    assert commands(engine, "accepted") == 1 and commands(engine, "rejected") == 1
    assert len(sandbox.journal()) == 1 and engine.last_skips == {}
    assert "already" in next(e for e in events(sent(publisher)) if e["event"] == "rejected")["detail"]  # fmt: skip


async def test_commands_are_off_by_default(sandbox):
    config = sandbox.config(mqtt={"enabled": True})
    publisher = MqttPublisher(config.mqtt, Metrics())
    engine = await sandbox.engine(config, mqtt=publisher)
    connect_mqtt_features(config, engine, publisher, engine.metrics)
    assert publisher.subscriptions == [] and publisher.on_message is None and publisher.birth == []
    assert config.mqtt.commands.operations == ["backup", "check"]


async def test_start_replaces_a_stale_running_state(sandbox):
    engine, publisher = await controlled(sandbox, {"check": {"enabled": True}})
    await engine.start()
    states = {t: p for t, p, retain in sent(publisher) if t.endswith("/state") and retain}
    assert states == {
        "rustic-scheduler/jobs/docs/backup/state": "idle",
        "rustic-scheduler/jobs/docs/check/state": "idle",
    }
    await engine.shutdown()


# ------------------------------------------------------------ configuration


def test_configuration_errors(sandbox):
    def problems(**mqtt) -> str:
        with pytest.raises(ConfigError) as info:
            sandbox.config(mqtt=mqtt)
        return "\n".join(info.value.problems)

    # An operation nothing would start is an error, unless MQTT may start it.
    on_request = {"prune": {"enabled": True}, "forget": {"enabled": True, "keep": {"keep-last": 1}, "after_backup": False}}  # fmt: skip
    docs = {"docs": {"profiles": ["repo"], "sources": ["/data"]} | on_request}
    with pytest.raises(ConfigError) as info:
        sandbox.config(docs, mqtt={"enabled": True, "commands": {"enabled": True}})
    assert len(info.value.problems) == 2 and "on request only" in info.value.problems[0]
    listed = {"enabled": True, "operations": ["forget", "prune"]}
    sandbox.config(docs, mqtt={"enabled": True, "commands": listed})

    assert "mqtt.commands.enabled needs mqtt.enabled" in problems(commands={"enabled": True})
    assert "mqtt.homeassistant.enabled needs" in problems(homeassistant={"enabled": True})
    assert "mqtt.commands.operations" in problems(commands={"operations": ["restore"]})
    assert "node_id" in problems(homeassistant={"node_id": "a/b"})
    assert "discovery_prefix" in problems(homeassistant={"discovery_prefix": "ha/#"})

    job = {"profiles": ["repo"], "sources": ["/data"]}
    with pytest.raises(ConfigError) as info:
        sandbox.config(
            {"a.b": job, "a_b": job | {"label": "x"}},
            mqtt={"enabled": True, "homeassistant": {"enabled": True}},
        )
    assert "cannot tell this job" in info.value.problems[0]


# ----------------------------------------------------------- home assistant


def discovery(mqtt: dict, operations, web: WebConfig | None = None) -> dict[str, dict | None]:
    config = MqttConfig(enabled=True, **mqtt)
    return {
        topic: json.loads(payload) if payload else None
        for topic, payload in discovery_messages(config, operations, web or WebConfig())
    }


def test_discovery_describes_enabled_operations_and_removes_the_rest():
    found = discovery(
        {"homeassistant": {"enabled": True}, "topic_prefix": "home/backup"},
        {"my.docs": [Operation.BACKUP, Operation.SNAPSHOTS]},
    )
    base = "homeassistant/sensor/rustic_scheduler/my_docs_"
    outcome = found[base + "backup_outcome/config"]
    assert outcome["state_topic"] == "home/backup/jobs/my.docs/backup/result"
    assert outcome["unique_id"] == "rustic_scheduler_my_docs_backup_outcome"
    assert outcome["availability_topic"] == "home/backup/status"
    assert outcome["device"]["identifiers"] == ["rustic_scheduler_my_docs"]
    assert outcome["device"]["name"] == "Rustic Scheduler my.docs"
    state = found[base + "backup_state/config"]
    assert state["state_topic"] == "home/backup/jobs/my.docs/backup/state"
    assert state["options"] == ["idle", "queued", "running"]
    assert found[base + "backup_progress/config"]["expire_after"] >= 30
    assert found[base + "snapshots_count/config"]["name"] == "Snapshot count"
    # Operations that are off get an empty payload, which removes a leftover entity.
    assert found[base + "forget_outcome/config"] is None
    assert found[base + "check_state/config"] is None
    # No button without MQTT commands.
    assert found["homeassistant/button/rustic_scheduler/my_docs_backup_run/config"] is None
    # Every device shares one identity per job, and every unique_id is unique.
    configs = [c for c in found.values() if c]
    assert len({c["unique_id"] for c in configs}) == len(configs)


@pytest.mark.parametrize(
    ("web", "expected"),
    [
        (WebConfig(), None),
        (WebConfig(enabled=True), None),
        (WebConfig(enabled=False, public_url="https://backup.example/"), None),
        (WebConfig(enabled=True, public_url="https://backup.example/"), "https://backup.example/"),
    ],
)
def test_discovery_web_configuration_url(web, expected):
    found = discovery(
        {"homeassistant": {"enabled": True}, "commands": {"enabled": True}},
        {"docs": [Operation.BACKUP], "photos": [Operation.BACKUP]},
        web,
    )
    configs = [config for config in found.values() if config is not None]
    assert configs
    assert all(config["device"].get("configuration_url") == expected for config in configs)
    assert any("/button/" in topic and config for topic, config in found.items())
    assert any(config is None for config in found.values())


def test_discovery_buttons_follow_the_allowed_commands():
    found = discovery(
        {
            "homeassistant": {"enabled": True, "node_id": "nas", "discovery_prefix": "ha/"},
            "commands": {"enabled": True, "operations": ["backup", "forget"]},
        },
        {"docs": [Operation.BACKUP, Operation.CHECK, Operation.FORGET]},
    )
    button = found["ha/button/nas/docs_backup_run/config"]
    assert button["command_topic"] == "rustic-scheduler/jobs/docs/backup/run"
    assert button["payload_press"] == "run"
    assert found["ha/button/nas/docs_forget_run/config"] is not None
    assert found["ha/button/nas/docs_check_run/config"] is None  # enabled, but not startable
    assert found["ha/button/nas/docs_prune_run/config"] is None


async def test_discovery_is_published_on_every_connection(sandbox):
    engine, publisher = await controlled(
        sandbox, {"check": {"enabled": True}}, homeassistant={"enabled": True}
    )
    topics = {topic for topic, payload in publisher.birth if payload}
    assert "homeassistant/button/rustic_scheduler/docs_check_run/config" in topics
    assert "homeassistant/sensor/rustic_scheduler/docs_check_outcome/config" in topics
    assert not any("forget" in topic for topic in topics)
    assert parse_config(sandbox.last_raw).mqtt.homeassistant.enabled


async def test_discovery_uses_web_configuration_from_application(sandbox):
    config = sandbox.config(
        mqtt={"enabled": True, "homeassistant": {"enabled": True}},
        web={"enabled": True, "public_url": "https://backup.example:8443/"},
    )
    publisher = MqttPublisher(config.mqtt, Metrics())
    engine = await sandbox.engine(config, mqtt=publisher)
    connect_mqtt_features(config, engine, publisher, engine.metrics)
    configs = [json.loads(payload) for _, payload in publisher.birth if payload]
    assert configs
    assert all(
        item["device"]["configuration_url"] == "https://backup.example:8443/" for item in configs
    )
