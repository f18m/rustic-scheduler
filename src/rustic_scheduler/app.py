"""Start-up: validate everything with the real Rustic binary, then run."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from . import SUPPORTED_RUSTIC_VERSION, __version__
from .commands import JobRuntime, build_env, make_runtime
from .config import Config, ConfigError, load_config
from .engine import Engine
from .homeassistant import discovery_messages
from .httpserver import HttpServer
from .metrics import Metrics
from .mqtt import MqttPublisher
from .mqtt_commands import CommandHandler
from .notify import Notifier
from .profiles import ProfileError, analyse, profile_args, show_config
from .redact import RedactingFilter
from .state import StateStore
from .web import WebServer

log = logging.getLogger(__name__)

#: Copy of the Rustic binary carrying the CAP_DAC_READ_SEARCH file capability.
PRIVILEGED_BINARY = Path("/usr/local/libexec/rustic-scheduler/rustic")
_CAP_DAC_READ_SEARCH = 2


def _bounding_set_has(capability: int) -> bool:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapBnd:"):
                return bool(int(line.split()[1], 16) >> capability & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def resolve_binary(config: Config) -> str:
    """Pick the Rustic binary.

    An explicit ``rustic.binary`` wins. Otherwise the read-everything copy is
    used when the container was granted CAP_DAC_READ_SEARCH (executing it
    without that capability would fail), else plain ``rustic`` from PATH.
    """
    if config.rustic.binary:
        return config.rustic.binary
    if PRIVILEGED_BINARY.is_file() and _bounding_set_has(_CAP_DAC_READ_SEARCH):
        return str(PRIVILEGED_BINARY)
    found = shutil.which("rustic")
    if found is None:
        raise ConfigError(
            ["rustic.binary: no `rustic` executable found in PATH; install Rustic or set the path"]
        )
    return found


async def rustic_version(binary: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    except (OSError, TimeoutError) as exc:
        raise ConfigError([f"rustic.binary: cannot run {binary}: {exc}"]) from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:200]
        raise ConfigError([f"rustic.binary: `{binary} --version` failed: {detail}"])
    return stdout.decode().strip().removeprefix("rustic").strip().removeprefix("v")


async def prepare(config: Config) -> tuple[dict[str, JobRuntime], str, list[str]]:
    """Validate every job against Rustic. Returns runtimes, version and warnings."""
    binary = resolve_binary(config)
    version = await rustic_version(binary)
    warnings: list[str] = []
    problems: list[str] = []
    if version != SUPPORTED_RUSTIC_VERSION:
        warnings.append(
            f"Rustic {version} is in use, but this release was verified against "
            f"{SUPPORTED_RUSTIC_VERSION}; progress and result parsing may differ"
        )
    runtimes: dict[str, JobRuntime] = {}
    for name, job in config.jobs.items():
        try:
            # Inspected without the scheduler's own Pushgateway URL, so that a
            # URL found in the result is known to come from the user.
            raw = await show_config(binary, profile_args(config, job), build_env(config, job, None))
        except ProfileError as exc:
            problems.append(f"jobs.{name}: {exc}")
            continue
        merged = analyse(name, job, config.label_of(name), raw)
        problems += merged.problems
        warnings += merged.warnings
        if merged.prometheus_url and config.pushgateway.url:
            ours = config.pushgateway.url.rstrip("/")
            if merged.prometheus_url.rstrip("/") != ours:
                warnings.append(
                    f"jobs.{name}: the profiles push Rustic's metrics to their own Pushgateway; "
                    "the scheduler leaves that setting alone and cannot verify delivery there"
                )
        runtimes[name] = make_runtime(config, name, binary, merged)
    if problems:
        raise ConfigError(problems)
    return runtimes, version, warnings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO", style: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        _JsonFormatter()
        if style == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def connect_mqtt_features(
    config: Config, engine: Engine, mqtt: MqttPublisher, metrics: Metrics
) -> None:
    """Enable MQTT commands and Home Assistant discovery where configured."""
    if config.mqtt.commands.enabled:
        handler = CommandHandler(engine, mqtt, config.mqtt.commands, metrics)
        handler.attach(mqtt)
        log.info(
            "MQTT commands enabled on %s for: %s",
            handler.topic_filter,
            ", ".join(config.mqtt.commands.operations) or "nothing",
        )
    if config.mqtt.homeassistant.enabled:
        operations = {name: engine.enabled_operations(name) for name in engine.runtimes}
        mqtt.birth = discovery_messages(config.mqtt, operations)


async def serve(config_path: Path) -> int:
    """Run the scheduler until SIGTERM or SIGINT."""
    try:
        config = load_config(config_path)
        setup_logging(config.scheduler.log_level, config.scheduler.log_format)
        runtimes, version, warnings = await prepare(config)
    except ConfigError as exc:
        setup_logging()
        for problem in exc.problems:
            log.error("configuration: %s", problem)
        log.error("not starting: fix the configuration problems above")
        return 2
    for warning in warnings:
        log.warning("configuration: %s", warning)

    metrics = Metrics()
    metrics.info.labels(__version__, version).set(1)
    state = StateStore(config.scheduler.state_dir, config.scheduler.run_logs_per_operation)
    mqtt = MqttPublisher(config.mqtt, metrics) if config.mqtt.enabled else None
    notifier = Notifier(config.notifications, metrics) if config.notifications.enabled else None
    engine = Engine(config, runtimes, metrics, state, mqtt=mqtt, notifier=notifier)
    server = HttpServer(engine, config.http.host, config.http.port, state.control_socket)
    web = WebServer(engine, config.web, version) if config.web.enabled else None
    if mqtt:
        connect_mqtt_features(config, engine, mqtt, metrics)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    mqtt_task = asyncio.create_task(mqtt.run(), name="mqtt") if mqtt else None
    log.info(
        "rustic-scheduler %s with Rustic %s (%s), %d job(s)",
        __version__,
        version,
        next(iter(runtimes.values())).binary,
        len(runtimes),
    )
    try:
        await server.start()
    except OSError as exc:
        log.error("cannot listen on %s:%s: %s", config.http.host, config.http.port, exc)
        if mqtt_task:
            mqtt_task.cancel()
        return 1
    if web:
        try:
            await web.start()
        except OSError as exc:
            log.error("cannot listen on %s:%s: %s", config.web.host, config.web.port, exc)
            await server.stop()
            if mqtt_task:
                mqtt_task.cancel()
            return 1
    await engine.start()
    await stop.wait()
    log.info("shutting down: stopping running operations")
    await engine.shutdown()
    if notifier:
        await notifier.drain()
    if mqtt and mqtt_task:
        await mqtt.stop(mqtt_task)
    if web:
        await web.stop()
    await server.stop()
    with contextlib.suppress(Exception):
        os.sync()
    log.info("stopped")
    return 0
