"""End-to-end paths through real side services: Pushgateway, Prometheus, MQTT, Apprise, S3."""

import asyncio
import json
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path

import aiomqtt
import pytest
from aiohttp import web
from prometheus_client.parser import text_string_to_metric_families

from helpers import SUMMARY, metric
from rustic_scheduler.config import MqttConfig
from rustic_scheduler.httpserver import HttpServer
from rustic_scheduler.metrics import Metrics
from rustic_scheduler.model import Operation, Outcome, Trigger
from rustic_scheduler.mqtt import MqttPublisher
from rustic_scheduler.notify import Notifier
from services import (
    MOSQUITTO_IMAGE,
    PROMETHEUS_IMAGE,
    PUSHGATEWAY_IMAGE,
    S3_IMAGE,
    Container,
)

pytestmark = pytest.mark.integration


async def run(engine, job="docs", operation=Operation.BACKUP):
    accepted, message = engine.submit(job, operation, Trigger.MANUAL)
    assert accepted, message
    await engine.wait_idle()
    return engine.last_results.get((job, operation))


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()


# -------------------------------------------------------------- pushgateway


@pytest.fixture
def pushgateway():
    container = Container(PUSHGATEWAY_IMAGE, 9091, host_gateway_access=True)
    try:
        container.wait_http("/-/ready")
        yield container
    finally:
        container.remove()


def pushed_samples(container) -> dict[str, list]:
    families = text_string_to_metric_families(fetch(f"http://127.0.0.1:{container.port}/metrics"))
    return {f.name: f.samples for f in families if f.name.startswith(("rustic_backup", "push_"))}


@pytest.mark.docker
async def test_rustic_pushes_its_own_metrics_and_delivery_is_verified(real, pushgateway):
    url = f"http://127.0.0.1:{pushgateway.port}"
    engine = await real.engine(real.config(pushgateway={"url": url}))
    result = await run(engine)

    assert result.outcome == Outcome.SUCCEEDED
    assert result.data["pushgateway"]["result"] == "delivered"
    assert (
        metric(engine.metrics, "rustic_scheduler_pushgateway_push_verified", {"job": "docs"}) == 1
    )
    delivered = {"job": "docs", "result": "delivered"}
    assert (
        metric(engine.metrics, "rustic_scheduler_pushgateway_verifications_total", delivered) == 1
    )

    # Scrape the real Pushgateway and validate what Rustic pushed.
    samples = pushed_samples(pushgateway)
    for name in ("rustic_backup_time", "rustic_backup_files_new", "rustic_backup_data_added",
                 "rustic_backup_total_bytes_processed", "rustic_backup_backup_duration"):  # fmt: skip
        assert name in samples, f"{name} was not pushed"
    (files_new,) = samples["rustic_backup_files_new"]
    assert files_new.value == result.data["files_new"] == 2
    # Grouping key chosen by Rustic 0.11.4.
    assert files_new.labels["job"] == "rustic_backup"
    assert files_new.labels["snapshot_label"] == "docs"
    assert files_new.labels["hostname"] == "testhost"
    assert files_new.labels["paths"] == str(real.source)
    assert "tags" not in files_new.labels  # empty values are not part of the key
    (pushed_at,) = samples["push_time_seconds"]
    assert pushed_at.value >= result.started_at.timestamp() - 1
    (failed_at,) = samples["push_failure_time_seconds"]
    assert failed_at.value == 0
    # The scheduler's own metric names never collide with Rustic's.
    own = engine.metrics.render().decode()
    assert "rustic_backup_" not in own


@pytest.mark.docker
async def test_failed_push_does_not_fail_the_backup_but_is_visible(real, pushgateway):
    url = f"http://127.0.0.1:{pushgateway.port}"
    engine = await real.engine(real.config(pushgateway={"url": url, "verify_timeout": 3}))
    pushgateway.stop()

    result = await run(engine)
    # Rustic logs a warning and exits 0; only the verification reveals the loss.
    assert result.outcome == Outcome.SUCCEEDED and result.exit_code == 0
    assert "error pushing metrics" in Path(result.log_file).read_text()
    assert result.data["pushgateway"]["result"] == "unreachable"
    assert (
        metric(engine.metrics, "rustic_scheduler_pushgateway_push_verified", {"job": "docs"}) == 0
    )

    # After a restart without persistence the Pushgateway is empty until the next push.
    pushgateway.start()
    pushgateway.wait_http("/-/ready")
    engine._pushgateway.url = f"http://127.0.0.1:{pushgateway.port}"
    assert "rustic_backup_time" not in pushed_samples(pushgateway)
    runtime = engine.runtimes["docs"]
    runtime.env["RUSTIC_PROMETHEUS"] = engine._pushgateway.url
    object.__setattr__(runtime, "pushgateway_url", engine.config.pushgateway.url)
    result = await run(engine)
    assert result.data["pushgateway"]["result"] == "delivered"
    assert (
        metric(engine.metrics, "rustic_scheduler_pushgateway_push_verified", {"job": "docs"}) == 1
    )


@pytest.mark.docker
async def test_stale_groups_stay_until_cleanup_is_enabled(real, pushgateway):
    url = f"http://127.0.0.1:{pushgateway.port}"
    other = real.root / "moved"
    other.mkdir()
    (other / "m.txt").write_text("moved")

    def groups() -> list[str]:
        data = json.loads(fetch(f"{url}/api/v1/metrics"))["data"]
        return sorted(
            g["labels"]["paths"] for g in data if g["labels"].get("snapshot_label") == "docs"
        )

    await run(await real.engine(real.config(pushgateway={"url": url})))
    # The job's sources change: Rustic pushes to a new group, the old one stays forever.
    moved = {"docs": real.job(sources=[str(other)])}
    await run(await real.engine(real.config(moved, pushgateway={"url": url})))
    assert groups() == sorted([str(other), str(real.source)])

    # Pushes within a second of the run's start count as belonging to it.
    await asyncio.sleep(1.5)
    cleanup = {"url": url, "cleanup_stale_groups": True}
    result = await run(await real.engine(real.config(moved, pushgateway=cleanup)))
    assert result.data["pushgateway"]["result"] == "delivered"
    assert groups() == [str(other)]


@pytest.mark.docker
async def test_a_profile_with_its_own_pushgateway_is_left_alone(real, pushgateway):
    mine = f"http://127.0.0.1:{pushgateway.port}"
    real.profile(
        "metrics", f'[global]\nprometheus = "{mine}"\n\n[backup]\nmetrics-job = "my_job"\n'
    )
    config = real.config(
        {"docs": real.job(profiles=["repo", "metrics"])}, pushgateway={"url": "http://127.0.0.1:9"}
    )
    engine = await real.engine(config)
    assert "RUSTIC_PROMETHEUS" not in engine.runtimes["docs"].env
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED and "pushgateway" not in result.data
    (sample,) = pushed_samples(pushgateway)["rustic_backup_files_new"]
    assert sample.labels["job"] == "my_job"  # the user's own settings were honoured


@pytest.mark.docker
async def test_prometheus_scrapes_both_endpoints_with_the_example_configuration(real, pushgateway):
    """A real Prometheus, the example scrape config, the scheduler and the Pushgateway."""
    engine = await real.engine(
        real.config(pushgateway={"url": f"http://127.0.0.1:{pushgateway.port}"})
    )
    server = HttpServer(engine, "0.0.0.0", 0, None)
    await engine.start()
    await server.start()
    scheduler_port = server._runners[0].addresses[0][1]
    example = (Path(__file__).parents[2] / "examples/prometheus/prometheus.yml").read_text()
    scrape = (
        example.replace("rustic-scheduler:9300", f"host.docker.internal:{scheduler_port}")
        .replace("pushgateway:9091", f"host.docker.internal:{pushgateway.port}")
        .replace("scrape_interval: 30s", "scrape_interval: 1s")
        .replace("  - /etc/prometheus/alerts.yml", "  - /etc/prometheus/alerts.yml")
    )
    config_file = real.root / "prometheus.yml"
    config_file.write_text(scrape)
    config_file.chmod(0o644)
    alerts = Path(__file__).parents[2] / "examples/prometheus/alerts.yml"
    prometheus = Container(
        PROMETHEUS_IMAGE, 9090,
        "-v", f"{config_file}:/etc/prometheus/prometheus.yml:ro",
        "-v", f"{alerts}:/etc/prometheus/alerts.yml:ro",
    )  # fmt: skip
    try:
        prometheus.wait_http("/-/ready", 60)
        result = await run(engine)
        assert result.outcome == Outcome.SUCCEEDED

        def query(expression: str) -> list:
            from urllib.parse import quote

            url = f"http://127.0.0.1:{prometheus.port}/api/v1/query?query={quote(expression)}"
            return json.loads(fetch(url))["data"]["result"]

        deadline = time.time() + 40
        own = pushed = []
        while time.time() < deadline and not (own and pushed):
            await asyncio.sleep(1)
            own = query(
                'rustic_scheduler_operation_last_outcome{outcome="succeeded",operation="backup"} == 1'
            )
            pushed = query("rustic_backup_files_new")
        assert own, f"scheduler metrics never arrived:\n{prometheus.logs()[-2000:]}"
        assert pushed, f"Rustic's pushed metrics never arrived:\n{prometheus.logs()[-2000:]}"
        # The scheduler's label does not clash with Prometheus' own `job` label.
        assert own[0]["metric"]["job"] == "rustic-scheduler"
        assert own[0]["metric"]["backup_job"] == "docs"
        assert "exported_job" not in own[0]["metric"]
        # honor_labels keeps the job label Rustic pushed.
        assert pushed[0]["metric"]["job"] == "rustic_backup"
        assert pushed[0]["metric"]["snapshot_label"] == "docs"
        assert float(pushed[0]["value"][1]) == 2
        # The example alert rules load.
        rules = json.loads(fetch(f"http://127.0.0.1:{prometheus.port}/api/v1/rules"))
        names = [r["name"] for g in rules["data"]["groups"] for r in g["rules"]]
        assert "RusticBackupNotSucceeded" in names and "RusticMetricsNotDelivered" in names
    finally:
        prometheus.remove()
        await server.stop()
        await engine.shutdown()


# --------------------------------------------------------------------- mqtt

BROKER_SETUP = (
    "rm -f /tmp/passwd && mosquitto_passwd -c -b /tmp/passwd scheduler s3cret-mqtt-pw && chown mosquitto /tmp/passwd && chmod 600 /tmp/passwd && "
    "printf 'listener 1883\\nallow_anonymous false\\npassword_file /tmp/passwd\\n"
    "listener 8883\\ncafile /certs/ca.pem\\ncertfile /certs/server.pem\\nkeyfile /certs/server.key\\n'"
    " > /tmp/m.conf && exec mosquitto -c /tmp/m.conf"
)


@pytest.fixture
def broker(short_tmp):
    certs = short_tmp / "certs"
    certs.mkdir()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-addext", "basicConstraints=critical,CA:TRUE",
         "-keyout", str(certs / "server.key"), "-out", str(certs / "server.pem")],
        check=True, capture_output=True,
    )  # fmt: skip
    (certs / "ca.pem").write_bytes((certs / "server.pem").read_bytes())
    for item in certs.iterdir():
        item.chmod(0o644)
    certs.chmod(0o755)
    container = Container(
        MOSQUITTO_IMAGE, 1883, "-p", "127.0.0.1::8883", "-v", f"{certs}:/certs:ro", "--user", "0",
        "--entrypoint", "sh", command=["-c", BROKER_SETUP], fixed_port=True,
    )  # fmt: skip
    container.tls_port = int(
        subprocess.run(["docker", "port", container.name, "8883"], capture_output=True, text=True, check=True)
        .stdout.splitlines()[0].rsplit(":", 1)[1]
    )  # fmt: skip
    container.ca = certs / "ca.pem"
    password = short_tmp / "mqtt_password"
    password.write_text("s3cret-mqtt-pw\n")
    container.password_file = password
    try:
        deadline = time.time() + 30
        while "mosquitto version" not in container.logs() and time.time() < deadline:
            time.sleep(0.3)
        time.sleep(0.5)
        yield container
    finally:
        container.remove()


async def collect(port: int, prefix: str, messages: list, ready: asyncio.Event) -> None:
    async with aiomqtt.Client(
        "127.0.0.1", port, username="scheduler", password="s3cret-mqtt-pw"
    ) as client:
        await client.subscribe(f"{prefix}/#", qos=1)
        ready.set()
        async for message in client.messages:
            messages.append((str(message.topic), message.payload.decode(), bool(message.retain)))


async def connected(publisher: MqttPublisher, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while not publisher.connected and time.time() < deadline:
        await asyncio.sleep(0.1)
    return publisher.connected


@pytest.mark.docker
async def test_mqtt_lifecycle_progress_and_results_with_authentication(real, broker):
    mqtt = {
        "enabled": True, "host": "127.0.0.1", "port": broker.port, "username": "scheduler",
        "password_file": str(broker.password_file), "topic_prefix": "test/backup", "progress_interval": 0,
    }  # fmt: skip
    config = real.config({"docs": real.job(snapshots={"enabled": True})}, mqtt=mqtt)
    metrics = Metrics()
    publisher = MqttPublisher(config.mqtt, metrics)
    publisher_task = asyncio.create_task(publisher.run())
    messages: list = []
    ready = asyncio.Event()
    collector = asyncio.create_task(collect(broker.port, "test/backup", messages, ready))
    await asyncio.wait_for(ready.wait(), 15)
    assert await connected(publisher)

    engine = await real.engine(config, mqtt=publisher)
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED
    await asyncio.sleep(1)

    by_topic: dict[str, list] = {}
    for topic, payload, _ in messages:
        by_topic.setdefault(topic, []).append(payload)
    assert by_topic["test/backup/status"][-1] == "online"
    events = [json.loads(p)["event"] for p in by_topic["test/backup/jobs/docs/backup/event"]]
    assert events == ["queued", "started", "finished"]
    progress = [json.loads(p) for p in by_topic["test/backup/jobs/docs/backup/progress"]]
    assert (
        progress[-1]["ratio"] == 1.0 and progress[-1]["bytes_done"] == progress[-1]["bytes_total"]
    )
    assert "eta_seconds" in progress[0] and progress[0]["eta_seconds"] is None  # unknown, not zero
    (final,) = [json.loads(p) for p in by_topic["test/backup/jobs/docs/backup/result"]]
    assert final["outcome"] == "succeeded" and final["data"]["files_new"] == 2
    (telemetry,) = [json.loads(p) for p in by_topic["test/backup/jobs/docs/snapshots/result"]]
    assert telemetry["outcome"] == "succeeded" and telemetry["data"]["count"] == 1
    assert all("s3cret-mqtt-pw" not in payload for _, payload, _ in messages)
    assert metric(metrics, "rustic_scheduler_mqtt_connected") == 1

    # Results are retained: a subscriber arriving later still learns the last outcome.
    late: list = []
    late_ready = asyncio.Event()
    late_task = asyncio.create_task(collect(broker.port, "test/backup", late, late_ready))
    await asyncio.wait_for(late_ready.wait(), 15)
    await asyncio.sleep(1)
    retained = {topic for topic, _, retain in late if retain}
    assert "test/backup/jobs/docs/backup/result" in retained and "test/backup/status" in retained
    assert "test/backup/jobs/docs/backup/event" not in retained

    # A clean stop announces `offline`.
    await publisher.stop(publisher_task)
    await asyncio.sleep(0.5)
    assert [p for t, p, _ in late if t == "test/backup/status"][-1] == "offline"
    for task in (collector, late_task):
        task.cancel()


@pytest.mark.docker
async def test_mqtt_reconnects_and_wrong_credentials_never_block_backups(real, broker):
    wrong = real.root / "wrong"
    wrong.write_text("not-the-password")
    bad = MqttConfig(enabled=True, host="127.0.0.1", port=broker.port, username="scheduler",
                     password_file=wrong, connect_timeout=3)  # fmt: skip
    metrics = Metrics()
    publisher = MqttPublisher(bad, metrics)
    task = asyncio.create_task(publisher.run())
    engine = await real.engine(mqtt=publisher)
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED  # the broker rejected us; the backup did not care
    assert not publisher.connected
    assert (
        metric(metrics, "rustic_scheduler_mqtt_messages_total", {"result": "connection_error"}) >= 1
    )
    await publisher.stop(task, timeout=1)

    good = MqttConfig(enabled=True, host="127.0.0.1", port=broker.port, username="scheduler",
                      password_file=broker.password_file, connect_timeout=3, max_reconnect_delay=1)  # fmt: skip
    metrics = Metrics()
    publisher = MqttPublisher(good, metrics)
    task = asyncio.create_task(publisher.run())
    assert await connected(publisher)
    broker.stop()
    deadline = time.time() + 20
    while publisher.connected and time.time() < deadline:
        publisher.publish("rustic-scheduler/probe", {"n": 1})
        await asyncio.sleep(0.3)
    assert not publisher.connected and metric(metrics, "rustic_scheduler_mqtt_connected") == 0
    publisher.publish("rustic-scheduler/jobs/docs/backup/result", {"queued": "while offline"})
    broker.start()
    assert await connected(publisher, 30), "the publisher did not reconnect"
    await publisher.stop(task)


@pytest.mark.docker
async def test_mqtt_over_tls_verifies_the_broker_certificate(broker):
    def config(**tls) -> MqttConfig:
        return MqttConfig(
            enabled=True, host="127.0.0.1", port=broker.tls_port, username="scheduler",
            password_file=broker.password_file, connect_timeout=5, tls={"enabled": True, **tls},
        )  # fmt: skip

    trusted = MqttPublisher(config(ca_file=str(broker.ca)), Metrics())
    task = asyncio.create_task(trusted.run())
    assert await connected(trusted), "TLS connection with the right CA failed"
    await trusted.stop(task)

    # Without the CA the self-signed broker certificate must be rejected.
    untrusted = MqttPublisher(config(), Metrics())
    task = asyncio.create_task(untrusted.run())
    assert not await connected(untrusted, 4)
    await untrusted.stop(task, timeout=1)
    assert isinstance(untrusted._tls_context(), ssl.SSLContext)


# ------------------------------------------------------------------ apprise


@pytest.fixture
async def webhook():
    received: list = []

    async def hook(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/hook", hook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield runner.addresses[0][1], received
    await runner.cleanup()


async def test_apprise_delivers_success_and_failure_notifications(real, webhook):
    port, received = webhook
    conf = real.root / "apprise.conf"
    conf.write_text(f"json://127.0.0.1:{port}/hook\n")
    config = real.config(
        {"good": real.job(), "bad": real.job(sources=[str(real.root / "missing")])},
        notifications={"enabled": True, "config_files": [str(conf)], "retries": 0},
    )
    metrics = Metrics()
    notifier = Notifier(config.notifications, metrics)
    engine = await real.engine(config, notifier=notifier)
    await run(engine, "good")
    await run(engine, "bad")
    await notifier.drain()

    titles = {item["title"]: item for item in received}
    assert set(titles) == {"backup succeeded: good", "backup failed: bad"}
    success = titles["backup succeeded: good"]
    assert success["type"] == "success" and "2 new, 0 changed" in success["message"]
    failure = titles["backup failed: bad"]
    assert failure["type"] == "failure" and "Reason: exit_code" in failure["message"]
    assert "integration-test-password" not in json.dumps(received)
    sent = {"event": "backup_success", "result": "sent"}
    assert metric(metrics, "rustic_scheduler_notifications_total", sent) == 1


async def test_undeliverable_notification_is_observable_and_leaves_the_backup_alone(real, caplog):
    conf = real.root / "apprise.conf"
    conf.write_text("json://user:hook-token-98765@127.0.0.1:9/hook\n")
    config = real.config(
        notifications={
            "enabled": True,
            "config_files": [str(conf)],
            "retries": 1,
            "retry_delay": 0,
            "timeout": 5,
        }
    )
    metrics = Metrics()
    notifier = Notifier(config.notifications, metrics)
    engine = await real.engine(config, notifier=notifier)
    result = await run(engine)
    await notifier.drain()
    assert result.outcome == Outcome.SUCCEEDED
    labels = {"job": "docs", "operation": "backup", "outcome": "succeeded"}
    assert metric(engine.metrics, "rustic_scheduler_operation_last_outcome", labels) == 1
    failed = {"event": "backup_success", "result": "failed"}
    assert metric(metrics, "rustic_scheduler_notifications_total", failed) == 1
    assert metric(metrics, "rustic_scheduler_notification_last_failure_timestamp_seconds") > 0
    assert "hook-token-98765" not in caplog.text


# ----------------------------------------------------------------------- s3


@pytest.fixture
def s3():
    container = Container(
        S3_IMAGE, 7070, "-e", "ROOT_ACCESS_KEY=test-access-key", "-e", "ROOT_SECRET_KEY=test-secret-key-12345",
        command=["posix", "/tmp"],
    )  # fmt: skip
    try:
        container.wait_http("/")
        container.exec("mkdir", "-p", "/tmp/backup-bucket")
        yield container
    finally:
        container.remove()


def s3_profile(real, s3) -> None:
    real.profile(
        "s3",
        f"""
[repository]
repository = "opendal:s3"
repo-hot = "opendal:s3"
password-file = "{real.password_file}"
no-cache = true

[repository.options]
bucket = "backup-bucket"
endpoint = "http://127.0.0.1:{s3.port}"
region = "us-east-1"

[repository.options-hot]
root = "/hot"

[repository.options-cold]
root = "/cold"
""",
    )


@pytest.mark.docker
async def test_s3_hot_cold_repository_with_a_shared_credentials_file(real, s3):
    """Hot/cold layout and file-based AWS credentials against a local S3 server.

    This does not exercise AWS, Glacier storage classes or restores from archive.
    """
    s3_profile(real, s3)
    credentials = real.root / "aws_credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = test-access-key\naws_secret_access_key = test-secret-key-12345\n"
    )
    credentials.chmod(0o600)
    aws_env = {
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    subprocess.run(
        [real.binary, "-P", str(real.profiles / "s3.toml"), "init"],
        env=real.env | aws_env, check=True, capture_output=True,
    )  # fmt: skip
    job = real.job(
        profiles=["s3"], snapshots={"enabled": True}, check={"enabled": True, "after_backup": True}
    )
    config = real.config({"docs": job}, rustic={"env": real.env | aws_env})
    engine = await real.engine(config)
    assert engine.runtimes["docs"].merged.repo_hot == "opendal:s3"
    result = await run(engine)
    assert result.outcome == Outcome.SUCCEEDED, result.detail
    assert engine.last_results[("docs", Operation.CHECK)].outcome == Outcome.SUCCEEDED
    assert metric(engine.metrics, "rustic_scheduler_snapshots", {"job": "docs"}) == 1

    listing = s3.exec(
        "sh", "-c", "cd /tmp/backup-bucket && find . -type f | cut -d/ -f2,3 | sort | uniq -c"
    )
    kinds = {line.split()[1]: int(line.split()[0]) for line in listing.splitlines()}
    # Cold holds everything; hot holds metadata and tree packs, but not file contents.
    for kind in ("config", "keys", "snapshots", "index", "data"):
        assert f"cold/{kind}" in kinds and f"hot/{kind}" in kinds, kinds
    assert kinds["cold/data"] > kinds["hot/data"]
    # The secret never appears in logs or results.
    assert "test-secret-key-12345" not in Path(result.log_file).read_text()
    assert "test-secret-key-12345" not in json.dumps(engine.status())

    # Without credentials the same job fails cleanly instead of hanging.
    no_creds = aws_env | {"AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}
    engine = await real.engine(real.config({"docs": job}, rustic={"env": real.env | no_creds}))
    result = await run(engine)
    assert result.outcome == Outcome.FAILED and "signing http request" in result.detail

    # What Rustic hands to a warm-up command on an S3 backend (used in docs/restore.md).
    recorder = real.root / "warmup.sh"
    recorder.write_text(f'#!/bin/sh\necho "$@" >> {real.root}/warmup.log\n')
    recorder.chmod(0o755)
    target = real.root / "restored"
    subprocess.run(
        [real.binary, "-P", str(real.profiles / "s3.toml"), "restore", "--warm-up-command",
         f"{recorder} %id %path", "latest", str(target)],
        env=real.env | aws_env, check=True, capture_output=True,
    )  # fmt: skip
    assert (next(target.rglob("a.txt"))).read_text() == "alpha\n"
    pack_id, path = (real.root / "warmup.log").read_text().split()[:2]
    print(f"S3 warm-up placeholders: %id={pack_id} %path={path}")
    assert len(pack_id) == 64 and path.endswith(f"data/{pack_id[:2]}/{pack_id}")


async def recording_proxy(target_port: int, seen: list) -> asyncio.Server:
    """Transparent TCP proxy that notes the request line and storage class of each request."""

    async def pump(reader, writer, sniff: bool) -> None:
        try:
            while data := await reader.read(65536):
                if sniff:
                    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
                    lines = head.split("\r\n")
                    if lines[0].startswith(("PUT ", "POST ")):
                        storage = [
                            l.split(":", 1)[1].strip()
                            for l in lines
                            if l.lower().startswith("x-amz-storage-class:")
                        ]
                        seen.append(
                            (
                                lines[0].split(" ")[0],
                                lines[0].split(" ")[1].split("?")[0],
                                storage[0] if storage else None,
                            )
                        )
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def handle(reader, writer) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
        await asyncio.gather(
            pump(reader, upstream_writer, True), pump(upstream_reader, writer, False)
        )

    return await asyncio.start_server(handle, "127.0.0.1", 0)


@pytest.mark.docker
async def test_deep_archive_storage_class_is_requested_for_cold_objects_only(real, s3):
    """What Rustic asks S3 for with `default_storage_class = "DEEP_ARCHIVE"` on the cold part.

    Proves the request Rustic sends, using a local S3 server. It does not prove
    how AWS stores, bills or restores such objects.
    """
    seen: list = []
    proxy = await recording_proxy(s3.port, seen)
    proxy_port = proxy.sockets[0].getsockname()[1]
    s3_profile(real, s3)
    profile = real.profiles / "s3.toml"
    profile.write_text(
        profile.read_text().replace(f":{s3.port}", f":{proxy_port}")
        + 'default_storage_class = "DEEP_ARCHIVE"\n'
    )
    credentials = real.root / "aws_credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = test-access-key\naws_secret_access_key = test-secret-key-12345\n"
    )
    aws_env = {
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    try:
        init = await asyncio.create_subprocess_exec(
            real.binary, "-P", str(profile), "init", env=real.env | aws_env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        _, stderr = await init.communicate()
        assert init.returncode == 0, stderr.decode()[-600:]
        config = real.config(
            {"docs": real.job(profiles=["s3"])}, rustic={"env": real.env | aws_env}
        )
        result = await run(await real.engine(config))
        assert result.outcome == Outcome.SUCCEEDED, result.detail
    finally:
        proxy.close()

    cold = [(path, storage) for _, path, storage in seen if "/cold/" in path]
    hot = [(path, storage) for _, path, storage in seen if "/hot/" in path]
    assert cold and hot
    assert {storage for _, storage in cold} == {"DEEP_ARCHIVE"}, cold
    assert {storage for _, storage in hot} == {None}, hot
    assert any("/cold/data/" in path for path, _ in cold)
    assert any("/hot/snapshots/" in path for path, _ in hot)


# ------------------------------------------------------- daemon end to end


def cli(config: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    import sys

    return subprocess.run(
        [sys.executable, "-m", "rustic_scheduler", "-c", str(config), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip


def start_daemon(config: Path, port: int):
    import sys

    process = subprocess.Popen(
        [sys.executable, "-m", "rustic_scheduler", "-c", str(config), "run"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if json.loads(fetch(f"http://127.0.0.1:{port}/readyz"))["ready"]:
                return process
        except OSError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError(f"daemon did not become ready:\n{process.stdout.read()}")


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_daemon_endpoints_manual_trigger_shutdown_and_restart(real):
    import signal

    import yaml

    port = free_port()
    job = real.job(schedule={"days": "daily", "times": ["03:00"]}, snapshots={"enabled": True})
    real.config({"docs": job}, http={"host": "127.0.0.1", "port": port})
    path = real.root / "config.yaml"
    path.write_text(yaml.safe_dump(real.last_raw))

    validation = cli(path, "validate")
    assert validation.returncode == 0 and "valid" in validation.stdout, validation.stderr
    shown = cli(path, "show-command", "docs", "backup").stdout
    assert "--json-progress" in shown and "--label docs" in shown

    daemon = start_daemon(path, port)
    try:
        assert cli(path, "healthcheck").returncode == 0
        before = fetch(f"http://127.0.0.1:{port}/metrics")
        assert "rustic_scheduler_ready 1.0" in before
        assert (
            'rustic_scheduler_next_run_timestamp_seconds{backup_job="docs",operation="backup"}'
            in before
        )
        # Nothing ran yet, so no outcome is claimed.
        assert "rustic_scheduler_operation_last_outcome{" not in before

        refused = cli(path, "trigger", "docs", "forget")
        assert refused.returncode == 1 and "not enabled" in refused.stdout
        triggered = cli(path, "trigger", "docs", "--wait")
        assert triggered.returncode == 0 and "backup succeeded" in triggered.stdout, (
            triggered.stdout
        )
        status = cli(path, "status")
        assert "last: succeeded" in status.stdout and "snapshots" in status.stdout
        after = fetch(f"http://127.0.0.1:{port}/metrics")
        succeeded = 'rustic_scheduler_operation_last_outcome{backup_job="docs",operation="backup",outcome="succeeded"} 1.0'
        assert succeeded in after
        assert 'rustic_scheduler_snapshots{backup_job="docs"} 1.0' in after
        api = json.loads(fetch(f"http://127.0.0.1:{port}/api/v1/status"))
        assert api["jobs"]["docs"]["operations"]["backup"]["last_result"]["outcome"] == "succeeded"

        manual = cli(path, "rustic", "docs", "--", "snapshots", "--json")
        assert (
            manual.returncode == 0
            and json.loads(manual.stdout)[0]["snapshots"][0]["label"] == "docs"
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        output = daemon.communicate(timeout=30)[0]
    assert daemon.returncode == 0, output
    assert "stopped" in output and "integration-test-password" not in output
    with pytest.raises(OSError):
        fetch(f"http://127.0.0.1:{port}/readyz")
    assert cli(path, "healthcheck").returncode == 1

    # After a restart the last results are still reported.
    daemon = start_daemon(path, port)
    try:
        assert succeeded in fetch(f"http://127.0.0.1:{port}/metrics")
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.communicate(timeout=30)


def test_daemon_refuses_to_start_with_a_broken_configuration(real):
    import yaml

    real.profile("labelled", '[backup]\nlabel = "other"\n')
    real.config({"docs": real.job(profiles=["repo", "labelled"])}, http={"port": free_port()})
    path = real.root / "config.yaml"
    path.write_text(yaml.safe_dump(real.last_raw))
    done = cli(path, "run", timeout=30)
    assert done.returncode == 2
    assert "label = 'other'" in done.stderr and "not starting" in done.stderr
    assert real.snapshots() == []


def test_summary_fixture_matches_the_real_binary(real):
    """The unit tests' canned summary has exactly the fields Rustic 0.11.4 emits."""
    done = subprocess.run(
        [real.binary, "-P", str(real.profiles / "repo.toml"), "--json-progress", "backup", str(real.source)],
        env=real.env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    messages = [json.loads(line) for line in done.stdout.splitlines()]
    summary = next(m for m in messages if m["message_type"] == "summary")
    assert set(summary) == set(SUMMARY)
    status = next(m for m in messages if m["message_type"] == "status")
    assert set(status) <= {"message_type", "seconds_elapsed", "seconds_remaining", "percent_done",
                           "total_bytes", "bytes_done"}  # fmt: skip
    assert 0 <= status["percent_done"] <= 1


@pytest.mark.docker
async def test_push_is_verified_even_when_no_snapshot_is_saved(real, pushgateway):
    url = f"http://127.0.0.1:{pushgateway.port}"
    job = real.job(backup={"extra_args": ["--skip-if-unchanged"]})
    engine = await real.engine(real.config({"docs": job}, pushgateway={"url": url}))
    await run(engine)
    await asyncio.sleep(1.5)
    second = await run(engine)
    # Rustic 0.11.4 pushes its metrics for an unchanged source too.
    assert second.data["snapshots_saved"] == 0
    assert second.data["pushgateway"]["result"] == "delivered"
