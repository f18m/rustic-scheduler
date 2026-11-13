"""Optional notifications through the Apprise library.

Notification URLs contain tokens, so they are only ever read from mounted
configuration files, never from the scheduler's YAML, and are registered with
the redactor before anything is sent. Delivery runs in the background: it
cannot delay or fail a backup, and its result is tracked separately.
"""

from __future__ import annotations

import asyncio
import logging

import apprise

from .config import NotificationsConfig
from .durations import format_duration
from .metrics import Metrics
from .model import Operation, OperationResult, Outcome, utcnow
from .redact import REDACTOR

log = logging.getLogger(__name__)


def event_for(result: OperationResult) -> str:
    """Notification event name for a result."""
    kind = "backup" if result.operation == Operation.BACKUP else "maintenance"
    if result.outcome == Outcome.SUCCEEDED:
        return f"{kind}_success"
    if result.outcome == Outcome.SKIPPED:
        return f"{kind}_skipped"
    return f"{kind}_failure"


def render(result: OperationResult) -> tuple[str, str]:
    """Title and body. Contains no secrets, paths of sources or command lines."""
    title = f"{result.operation} {result.outcome}: {result.job}"
    lines = [f"Job: {result.job}", f"Operation: {result.operation}", f"Outcome: {result.outcome}"]
    if result.reason != "none":
        lines.append(f"Reason: {result.reason}")
    lines.append(f"Trigger: {result.trigger}")
    if result.duration_seconds is not None:
        lines.append(f"Duration: {format_duration(result.duration_seconds)}")
    if result.exit_code is not None and result.outcome != Outcome.SUCCEEDED:
        lines.append(f"Rustic exit status: {result.exit_code}")
    data = result.data
    if result.outcome == Outcome.SUCCEEDED and result.operation == Operation.BACKUP:
        lines.append(
            f"Files: {data.get('files_new', '?')} new, {data.get('files_changed', '?')} changed, "
            f"{data.get('files_unmodified', '?')} unmodified"
        )
        lines.append(f"Added to repository: {data.get('data_added_packed', '?')} bytes")
    if result.operation == Operation.FORGET and "removed" in data:
        verb = "would remove" if result.dry_run else "removed"
        lines.append(f"Snapshots: {verb} {data['removed']}, kept {data.get('kept', '?')}")
    if result.detail:
        lines.append(f"Detail: {result.detail}")
    return title, REDACTOR("\n".join(lines))


class Notifier:
    def __init__(self, config: NotificationsConfig, metrics: Metrics) -> None:
        self.config = config
        self.metrics = metrics
        self._tasks: set[asyncio.Task[bool]] = set()
        # Apprise logs target URLs at debug level.
        logging.getLogger("apprise").setLevel(logging.WARNING)

    def _load(self) -> apprise.Apprise:
        # Re-read on every notification, so rotated secrets are picked up.
        target = apprise.Apprise()
        source = apprise.AppriseConfig()
        for path in self.config.config_files:
            source.add(str(path))
        target.add(source)
        REDACTOR.register([str(server.url(privacy=False)) for server in target])  # type: ignore[no-untyped-call]
        return target

    def submit(self, result: OperationResult) -> None:
        """Send in the background if the event is one the user asked for."""
        event = event_for(result)
        if event not in self.config.events:
            return
        task = asyncio.create_task(self.send(event, result))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, event: str, result: OperationResult) -> bool:
        title, body = render(result)
        kind = (
            apprise.NotifyType.SUCCESS
            if result.outcome == Outcome.SUCCEEDED
            else apprise.NotifyType.WARNING
            if result.outcome == Outcome.SKIPPED
            else apprise.NotifyType.FAILURE
        )
        attempts = self.config.retries + 1
        for attempt in range(1, attempts + 1):
            problem = "delivery failed"
            try:
                target = self._load()
                if len(target) == 0:
                    problem = "no notification target is configured"
                else:
                    sent = await asyncio.wait_for(
                        target.async_notify(body=body, title=title, notify_type=kind),
                        self.config.timeout.total_seconds(),
                    )
                    if sent:
                        self.metrics.notifications.labels(event, "sent").inc()
                        return True
            except TimeoutError:
                problem = "timed out"
            except Exception as exc:
                problem = REDACTOR(f"{type(exc).__name__}: {exc}")
            log.warning(
                "notification %s for job %s failed (attempt %d of %d): %s",
                event,
                result.job,
                attempt,
                attempts,
                problem,
            )
            if attempt < attempts:
                await asyncio.sleep(self.config.retry_delay.total_seconds())
        self.metrics.notifications.labels(event, "failed").inc()
        self.metrics.notification_last_failure.set(utcnow().timestamp())
        return False

    async def drain(self, timeout: float = 10.0) -> None:
        """Give in-flight notifications a moment to finish at shutdown."""
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=timeout)
            for task in pending:
                task.cancel()
