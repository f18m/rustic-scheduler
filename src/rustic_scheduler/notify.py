"""Optional notifications through the Apprise library.

Notification URLs contain tokens, so they are only ever read from mounted
configuration files, never from the scheduler's YAML, and are registered with
the redactor before anything is sent. Delivery runs in the background: it
cannot delay or fail a backup, and its result is tracked separately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import apprise

from .config import NotificationsConfig
from .durations import format_duration
from .metrics import Metrics
from .model import Operation, OperationResult, Outcome, utcnow
from .redact import REDACTOR

log = logging.getLogger(__name__)

#: Event name of the start-up notification. It is not one of the `events` the user
#: selects: it is sent whenever notifications are enabled.
STARTED_EVENT = "scheduler_started"

#: Icon in front of each title, so the outcome shows at a glance in a notification list
#: or on a lock screen. Plain Unicode, so it reaches the text-only services as well.
ICONS = {
    apprise.NotifyType.SUCCESS: "✅",
    apprise.NotifyType.WARNING: "⚠️",
    apprise.NotifyType.FAILURE: "❌",
    apprise.NotifyType.INFO: "🚀",
}


def event_for(result: OperationResult) -> str:
    """Notification event name for a result."""
    kind = "backup" if result.operation == Operation.BACKUP else "maintenance"
    if result.outcome == Outcome.SUCCEEDED:
        return f"{kind}_success"
    if result.outcome == Outcome.SKIPPED:
        return f"{kind}_skipped"
    return f"{kind}_failure"


def body_text(lines: list[str], web_url: str | None) -> str:
    """Joins the lines, adds the link to the web interface if there is one, masks secrets."""
    if web_url is not None:
        lines = [*lines, "", f"Web UI: {web_url}"]
    return REDACTOR("\n".join(lines))


_PREFIXES = ("", "k", "M", "G", "T", "P")


def _human(value: object, unit: str = "") -> str:
    """A Rustic count in powers of 1000: ``359486`` is ``359k``, and with a unit ``86136747`` is
    ``86Mbytes``. Below 10 a scaled figure keeps one decimal (``1.5M``). A value that is not a
    whole number is ``?``, never zero.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "?"
    scaled, step = float(value), 0
    while scaled >= 999.5 and step < len(_PREFIXES) - 1:
        scaled /= 1000
        step += 1
    if step == 0:
        return f"{value} {unit}".strip()
    text = (f"{scaled:.1f}" if scaled < 10 else f"{scaled:.0f}").removesuffix(".0")
    return f"{text}{_PREFIXES[step]}{unit}"


def render(result: OperationResult, web_url: str | None = None) -> tuple[str, str]:
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
        new, changed, unmodified = (
            _human(data.get(key)) for key in ("files_new", "files_changed", "files_unmodified")
        )
        lines.append(f"Files: {new} new, {changed} changed, {unmodified} unmodified")
        lines.append(f"Added to repository: {_human(data.get('data_added_packed'), 'bytes')}")
    if result.operation == Operation.FORGET and "removed" in data:
        verb = "would remove" if result.dry_run else "removed"
        lines.append(
            f"Snapshots: {verb} {_human(data['removed'])}, kept {_human(data.get('kept'))}"
        )
    if result.detail:
        lines.append(f"Detail: {result.detail}")
    return title, body_text(lines, web_url)


class Notifier:
    def __init__(
        self, config: NotificationsConfig, metrics: Metrics, web_url: str | None = None
    ) -> None:
        self.config = config
        self.metrics = metrics
        #: Public URL of the web interface, linked from every notification. None when the
        #: web interface is off or has no `public_url`.
        self.web_url = web_url
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
        self._spawn(self.send(event, result))

    def submit_startup(self, scheduler_version: str, rustic_version: str, jobs: int) -> None:
        """Announce that the scheduler is running. Sent in the background."""
        body = body_text(
            [f"Version: {scheduler_version}", f"Rustic: {rustic_version}", f"Jobs: {jobs}"],
            self.web_url,
        )
        self._spawn(
            self._deliver(
                STARTED_EVENT,
                "startup",
                f"rustic-scheduler {scheduler_version} started",
                body,
                apprise.NotifyType.INFO,
            )
        )

    def _spawn(self, delivery: Coroutine[Any, Any, bool]) -> None:
        task = asyncio.create_task(delivery)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, event: str, result: OperationResult) -> bool:
        title, body = render(result, self.web_url)
        kind = (
            apprise.NotifyType.SUCCESS
            if result.outcome == Outcome.SUCCEEDED
            else apprise.NotifyType.WARNING
            if result.outcome == Outcome.SKIPPED
            else apprise.NotifyType.FAILURE
        )
        return await self._deliver(event, f"job {result.job}", title, body, kind)

    async def _deliver(
        self,
        event: str,
        subject: str,
        title: str,
        body: str,
        kind: apprise.NotifyType,
    ) -> bool:
        title = f"{ICONS[kind]} {title}"
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
                "notification %s for %s failed (attempt %d of %d): %s",
                event,
                subject,
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
