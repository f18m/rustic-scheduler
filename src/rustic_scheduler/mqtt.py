"""Optional MQTT connection: publishing, and receiving command messages.

Publishing is fire-and-forget from the engine's point of view: messages go into
a bounded in-memory queue and a background task delivers them. A broker that is
slow, down or misconfigured can therefore never delay or fail a backup; it only
shows up in ``rustic_scheduler_mqtt_*`` metrics and in the log.

Incoming messages are handed to ``on_message``; what they mean is decided in
:mod:`rustic_scheduler.mqtt_commands`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiomqtt

from .config import MqttConfig
from .metrics import Metrics
from .redact import REDACTOR

log = logging.getLogger(__name__)


@dataclass
class _Message:
    topic: str
    payload: str
    qos: int
    retain: bool
    droppable: bool


class MqttPublisher:
    def __init__(self, config: MqttConfig, metrics: Metrics) -> None:
        self.config = config
        self.metrics = metrics
        self.connected = False
        self._queue: asyncio.Queue[_Message | None] = asyncio.Queue(maxsize=config.queue_size)
        self._pending: _Message | None = None
        self._stopping = False
        #: Topic filters subscribed to after every connection.
        self.subscriptions: list[str] = []
        #: Called with topic, payload and the retained flag of each received message.
        self.on_message: Callable[[str, bytes, bool], None] | None = None
        #: Retained messages published after every connection (topic, payload).
        self.birth: list[tuple[str, str]] = []
        metrics.mqtt_connected.set(0)

    def topic(self, *parts: str) -> str:
        return "/".join([self.config.topic_prefix.strip("/"), *parts])

    def publish(
        self,
        topic: str,
        payload: dict[str, Any] | str,
        *,
        retain: bool = False,
        droppable: bool = False,
    ) -> None:
        """Queue a message. Never blocks and never raises."""
        if droppable and not self.connected:
            self.metrics.mqtt_messages.labels("dropped").inc()
            return
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        message = _Message(
            topic, REDACTOR(text), 0 if droppable else self.config.qos, retain, droppable
        )
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.metrics.mqtt_messages.labels("dropped").inc()
            log.warning("MQTT queue is full; dropping a message for %s", topic)

    def _tls_context(self) -> ssl.SSLContext | None:
        tls = self.config.tls
        if not tls.enabled:
            return None
        context = ssl.create_default_context(cafile=str(tls.ca_file) if tls.ca_file else None)
        if tls.cert_file:
            context.load_cert_chain(str(tls.cert_file), str(tls.key_file) if tls.key_file else None)
        if tls.insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _password(self) -> str | None:
        # Read on every connection attempt, so a rotated secret is picked up.
        if self.config.password_file is None:
            return None
        password = self.config.password_file.read_text(encoding="utf-8").strip()
        REDACTOR.register([password])
        return password

    async def _receive(self, client: aiomqtt.Client) -> None:
        """Dispatch incoming messages. Ends with an error when the connection is lost."""
        async for message in client.messages:
            payload = message.payload
            if self.on_message is None or not isinstance(payload, bytes | bytearray):
                continue
            try:
                self.on_message(str(message.topic), bytes(payload), bool(message.retain))
            except Exception:
                log.exception("handling an MQTT message on %s failed", message.topic)
        raise aiomqtt.MqttError("the message stream ended")

    async def _next(self, reader: asyncio.Task[None]) -> _Message | None:
        """The next message to send; raises when the connection is lost meanwhile."""
        if self._pending is not None:
            return self._pending
        getter = asyncio.ensure_future(self._queue.get())
        try:
            await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        reader.result()  # raises the connection error
        raise aiomqtt.MqttError("the message stream ended")

    async def run(self) -> None:
        """Connect, deliver queued messages and reconnect with backoff, forever."""
        cfg = self.config
        delay = 1.0
        status = self.topic("status")
        while not self._stopping:
            try:
                client = aiomqtt.Client(
                    hostname=cfg.host,
                    port=cfg.effective_port,
                    username=cfg.username,
                    password=self._password(),
                    identifier=cfg.client_id,
                    tls_context=self._tls_context(),
                    keepalive=int(cfg.keepalive.total_seconds()),
                    timeout=cfg.connect_timeout.total_seconds(),
                    will=aiomqtt.Will(status, "offline", qos=1, retain=True),
                )
                async with client:
                    log.info("connected to MQTT broker %s:%s", cfg.host, cfg.effective_port)
                    self.connected = True
                    self.metrics.mqtt_connected.set(1)
                    delay = 1.0
                    for topic, payload in self.birth:
                        await client.publish(topic, REDACTOR(payload), qos=1, retain=True)
                    await client.publish(status, "online", qos=1, retain=True)
                    for topic_filter in self.subscriptions:
                        await client.subscribe(topic_filter, qos=1)
                    reader = asyncio.create_task(self._receive(client), name="mqtt-receive")
                    try:
                        while True:
                            message = await self._next(reader)
                            if message is None:
                                await client.publish(status, "offline", qos=1, retain=True)
                                return
                            self._pending = None if message.droppable else message
                            await client.publish(
                                message.topic,
                                message.payload,
                                qos=message.qos,
                                retain=message.retain,
                            )
                            self._pending = None
                            self.metrics.mqtt_messages.labels("published").inc()
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            except (aiomqtt.MqttError, OSError, ssl.SSLError) as exc:
                self.metrics.mqtt_messages.labels("connection_error").inc()
                log.warning(
                    "MQTT connection problem (%s); retrying in %.0fs", REDACTOR(str(exc)), delay
                )
            finally:
                self.connected = False
                self.metrics.mqtt_connected.set(0)
            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, cfg.max_reconnect_delay.total_seconds())

    async def stop(self, task: asyncio.Task[None], timeout: float = 5.0) -> None:
        """Flush what can be flushed quickly, announce `offline`, disconnect."""
        self._stopping = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            task.cancel()
        try:
            await asyncio.wait_for(task, timeout)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
