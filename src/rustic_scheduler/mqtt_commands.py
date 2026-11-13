"""Starting operations by MQTT message.

A message with the payload ``run`` on ``<prefix>/jobs/<job>/<operation>/run``
asks for that operation, exactly like ``rustic-scheduler trigger``. The same
rules apply (forget and prune must be enabled on the job) plus two more: the
operation must be listed in ``mqtt.commands.operations``, and retained messages
are ignored, so a message left on the broker cannot start a run at every
reconnect.

Anyone who may publish to the broker can use this. See docs/mqtt.md.
"""

from __future__ import annotations

import logging

from .config import MqttCommandsConfig
from .engine import Engine
from .metrics import Metrics
from .model import Operation, Trigger
from .mqtt import MqttPublisher

log = logging.getLogger(__name__)

PAYLOAD_RUN = "run"


class CommandHandler:
    def __init__(
        self,
        engine: Engine,
        publisher: MqttPublisher,
        config: MqttCommandsConfig,
        metrics: Metrics,
    ) -> None:
        self.engine = engine
        self.config = config
        self.metrics = metrics
        self._head = [*publisher.topic().split("/"), "jobs"]
        self.topic_filter = publisher.topic("jobs", "+", "+", "run")
        for result in ("accepted", "rejected", "invalid"):
            metrics.mqtt_commands.labels(result)

    def attach(self, publisher: MqttPublisher) -> None:
        publisher.subscriptions.append(self.topic_filter)
        publisher.on_message = self

    def _invalid(self, topic: str, why: str) -> None:
        self.metrics.mqtt_commands.labels("invalid").inc()
        log.warning("ignoring MQTT command on %s: %s", topic, why)

    def __call__(self, topic: str, payload: bytes, retained: bool) -> None:
        parts = topic.split("/")
        size = len(self._head)
        if len(parts) != size + 3 or parts[:size] != self._head or parts[-1] != "run":
            return  # not a command topic
        job, name = parts[size], parts[size + 1]
        if retained:
            self._invalid(topic, "retained messages are never acted on; clear it on the broker")
            return
        if payload.decode("utf-8", "replace").strip().lower() != PAYLOAD_RUN:
            self._invalid(topic, f"the payload must be `{PAYLOAD_RUN}`")
            return
        if job not in self.engine.runtimes:
            self._invalid(topic, "unknown job")
            return
        try:
            operation = Operation(name)
        except ValueError:
            self._invalid(topic, "unknown operation")
            return

        refusal = None
        state = self.engine.is_active(job, operation)
        if operation.value not in self.config.operations:
            refusal = f"{operation} is not listed in mqtt.commands.operations"
        elif state is not None:
            # Not recorded as a skipped run: a repeated message is not a missed backup.
            refusal = f"{operation} for job {job} is already {state}"
        else:
            accepted, message = self.engine.trigger(job, operation, Trigger.MQTT)
            if not accepted:
                refusal = message
        if refusal is None:
            self.metrics.mqtt_commands.labels("accepted").inc()
            log.info("job %s: %s requested by MQTT message", job, operation)
        else:
            self.metrics.mqtt_commands.labels("rejected").inc()
            self.engine.reject(job, operation, Trigger.MQTT, refusal)
