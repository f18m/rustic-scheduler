"""Scheduling loop and operation pipelines.

Rules implemented here (documented for users in docs/behaviour.md):

* One operation at a time per repository. Operations on the same repository
  queue in arrival order; a run that waits longer than
  ``repository_wait_timeout`` is skipped.
* A (job, operation) that is already queued or running is never queued twice:
  the newer request is skipped.
* A backup counts as succeeded only when Rustic exits with status 0, was not
  interrupted and reported a summary.
* After a backup: check, then forget, then snapshot telemetry, each only if
  enabled with ``after_backup``. Check and forget need a succeeded backup, and
  forget is not run when that check did not succeed. Telemetry always runs.
* Telemetry, MQTT and notification results never change a Rustic outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import commands
from .commands import JobRuntime
from .config import Config
from .locks import LockTimeout, RepositoryLocks
from .metrics import Metrics
from .model import Operation, OperationResult, Outcome, Progress, Reason, Trigger, utcnow
from .mqtt import MqttPublisher
from .notify import Notifier
from .pushgateway import PushgatewayClient
from .redact import REDACTOR
from .runner import ProcessResult, run_process
from .schedule import Schedule
from .state import StateStore

log = logging.getLogger(__name__)

#: Longest sleep of the loop; bounds how late a clock jump is noticed.
TICK = 5.0
#: A run that starts this late is still an on-time scheduled run.
ON_TIME = timedelta(seconds=60)
_FRACTION = re.compile(r"(\.\d{6})\d+")
_NO_RETRY = {Reason.TIMEOUT, Reason.SCOPE_VIOLATION, Reason.REMOVAL_LIMIT, Reason.SPAWN_ERROR}
_AFTER_BACKUP_ORDER = (Operation.CHECK, Operation.FORGET, Operation.SNAPSHOTS)


@dataclass
class Attempt:
    """What one execution of an operation established."""

    outcome: Outcome
    reason: Reason = Reason.NONE
    exit_code: int | None = None
    detail: str = ""
    dry_run: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleEntry:
    job: str
    operation: Operation
    schedule: Schedule
    policy: str
    grace: timedelta
    next_fire: datetime


def parse_time(value: str) -> datetime:
    """Parse a Rustic timestamp (RFC 3339, possibly with nanoseconds)."""
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value))


def classify(process: ProcessResult) -> Attempt | None:
    """Failure or interruption visible from the process alone; ``None`` if clean."""
    code = process.exit_code
    if process.spawn_error:
        return Attempt(Outcome.FAILED, Reason.SPAWN_ERROR, detail=process.spawn_error)
    if process.stopped_by_scheduler:
        return Attempt(
            Outcome.INTERRUPTED, Reason.SHUTDOWN, code, "stopped because the scheduler shut down"
        )
    if process.timed_out:
        return Attempt(Outcome.FAILED, Reason.TIMEOUT, code, "stopped after reaching its timeout")
    if process.saw_interrupt or code in (-signal.SIGTERM, -signal.SIGINT):
        return Attempt(
            Outcome.INTERRUPTED, Reason.SIGNAL, code, "Rustic received a termination signal"
        )
    if code is None or code < 0:
        return Attempt(Outcome.FAILED, Reason.KILLED, code, f"Rustic was killed (status {code})")
    if code != 0:
        return Attempt(Outcome.FAILED, Reason.EXIT_CODE, code, _error_lines(process))
    return None


def _error_lines(process: ProcessResult) -> str:
    noise = ("Backtrace", "disabled backtrace", "Some additional details")
    lines = [line.strip() for line in process.stderr_tail if line.strip()]
    lines = [line for line in lines if not line.startswith(noise)]
    errors = [line for line in lines if "ERROR" in line or "error" in line.lower()]
    chosen = errors[-3:] or lines[-3:]
    text = " | ".join(re.sub(r"\x1b\[[0-9;]*m", "", line) for line in chosen)
    return text[:600] or f"Rustic exited with status {process.exit_code}"


def summarise_backup(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Combine the `summary` messages of a backup; ``None`` if there is none."""
    summaries = [m for m in messages if m.get("message_type") == "summary"]
    if not summaries:
        return None
    data: dict[str, Any] = {}
    for summary in summaries:
        for key, value in summary.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                data[key] = data.get(key, 0) + value
    ids = [str(s["snapshot_id"]) for s in summaries if s.get("snapshot_id")]
    data["snapshots_saved"] = len(ids)
    data["snapshot_ids"] = [snapshot_id[:8] for snapshot_id in ids][:20]
    data["summaries"] = len(summaries)
    return data


def progress_from(message: dict[str, Any], now: datetime) -> Progress:
    """Map a Rustic `status` message; fields Rustic omits stay unknown."""

    def number(key: str) -> Any:
        value = message.get(key)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    return Progress(
        updated_at=now,
        elapsed_seconds=number("seconds_elapsed"),
        bytes_done=number("bytes_done"),
        bytes_total=number("total_bytes"),
        ratio=number("percent_done"),
        eta_seconds=number("seconds_remaining"),
    )


def _in_scope(snapshot: dict[str, Any], runtime: JobRuntime) -> bool:
    if snapshot.get("label") != runtime.label:
        return False
    return runtime.job.host is None or snapshot.get("hostname") == runtime.job.host


def summarise_snapshots(payload: Any, runtime: JobRuntime) -> dict[str, Any]:
    """Telemetry from `rustic snapshots --json`. Raises ``ValueError`` if unusable."""
    if not isinstance(payload, list):
        raise ValueError("unexpected JSON from `rustic snapshots`")
    snapshots = [s for group in payload for s in group.get("snapshots", [])]
    foreign = [s for s in snapshots if not _in_scope(s, runtime)]
    if foreign:
        raise LookupError(f"{len(foreign)} snapshot(s) outside the job's scope were returned")
    times = sorted((parse_time(s["time"]), s) for s in snapshots)
    data: dict[str, Any] = {"count": len(snapshots), "groups": len(payload)}
    if times:
        latest_time, latest = times[-1]
        data["latest"] = latest_time.timestamp()
        data["oldest"] = times[0][0].timestamp()
        data["latest_id"] = str(latest.get("id", ""))[:8]
        data["latest_bytes"] = (latest.get("summary") or {}).get("total_bytes_processed")
    else:
        data.update(latest=None, oldest=None, latest_bytes=None)
    return data


def summarise_forget(payload: Any, runtime: JobRuntime) -> dict[str, Any]:
    """Counts from `rustic forget --json`; raises ``LookupError`` on foreign snapshots."""
    if not isinstance(payload, list):
        raise ValueError("unexpected JSON from `rustic forget`")
    items = [item for group in payload for item in group.get("items", [])]
    foreign = [item for item in items if not _in_scope(item.get("snapshot") or {}, runtime)]
    if foreign:
        raise LookupError(f"{len(foreign)} snapshot(s) outside the job's scope were selected")
    removed = [item for item in items if not item.get("keep")]
    return {
        "removed": len(removed),
        "kept": len(items) - len(removed),
        "groups": len(payload),
        "removed_ids": [str(item["snapshot"].get("id", ""))[:8] for item in removed][:50],
    }


class Engine:
    def __init__(
        self,
        config: Config,
        runtimes: dict[str, JobRuntime],
        metrics: Metrics,
        state: StateStore,
        *,
        mqtt: MqttPublisher | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.runtimes = runtimes
        self.metrics = metrics
        self.state = state
        self.mqtt = mqtt
        self.notifier = notifier
        self.clock = clock
        self.locks = RepositoryLocks(state.lock_dir)
        self.stop_event = asyncio.Event()
        self.started_at = clock()
        self.heartbeat: datetime | None = None
        self.entries: list[ScheduleEntry] = []
        self.last_results: dict[tuple[str, Operation], OperationResult] = {}
        self.last_skips: dict[tuple[str, Operation], OperationResult] = {}
        self.progress: dict[tuple[str, Operation], Progress] = {}
        self._active: dict[tuple[str, Operation], str] = {}
        self._tasks: dict[asyncio.Task[None], tuple[str, Operation]] = {}
        self._progress_sent: dict[tuple[str, Operation], datetime] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._pushgateway = (
            PushgatewayClient(
                config.pushgateway.url, config.pushgateway.verify_timeout.total_seconds()
            )
            if config.pushgateway.url and config.pushgateway.verify
            else None
        )

    # ------------------------------------------------------------------ setup

    def enabled_operations(self, job: str) -> list[Operation]:
        cfg = self.runtimes[job].job
        operations = [Operation.BACKUP]
        operations += [
            operation
            for operation in (
                Operation.SNAPSHOTS,
                Operation.FORGET,
                Operation.PRUNE,
                Operation.CHECK,
            )
            if getattr(cfg, operation.value).enabled
        ]
        return operations

    def _build_entries(self, now: datetime) -> None:
        scheduler = self.config.scheduler
        for name, runtime in self.runtimes.items():
            cfg = runtime.job
            candidates = [
                (Operation.BACKUP, cfg.schedule, True),
                (Operation.SNAPSHOTS, cfg.snapshots.schedule, cfg.snapshots.enabled),
                (Operation.FORGET, cfg.forget.schedule, cfg.forget.enabled),
                (Operation.PRUNE, cfg.prune.schedule, cfg.prune.enabled),
                (Operation.CHECK, cfg.check.schedule, cfg.check.enabled),
            ]
            for operation, schedule_config, enabled in candidates:
                if schedule_config is None or not enabled:
                    continue
                schedule = schedule_config.build(scheduler.timezone)
                entry = ScheduleEntry(
                    job=name,
                    operation=operation,
                    schedule=schedule,
                    policy=cfg.missed_run_policy or scheduler.missed_run_policy,
                    grace=cfg.misfire_grace or scheduler.misfire_grace,
                    next_fire=schedule.next_after(now),
                )
                self.entries.append(entry)
                self.metrics.next_run.labels(name, operation).set(entry.next_fire.timestamp())
                log.info(
                    "job %s: %s scheduled %s; next run %s",
                    name,
                    operation,
                    schedule.describe(),
                    entry.next_fire.astimezone(schedule.tz).isoformat(timespec="minutes"),
                )

    async def start(self) -> None:
        """Restore state, plan the schedules and start the loop."""
        now = self.clock()
        self.metrics.start_time.set(self.started_at.timestamp())
        self.metrics.jobs.set(len(self.runtimes))
        for name in self.runtimes:
            for operation in self.enabled_operations(name):
                self.metrics.declare(name, operation)
        for result in self.state.load():
            if result.job not in self.runtimes:
                continue  # a job that was removed from the configuration
            self.last_results[(result.job, result.operation)] = result
            self.metrics.set_last_result(result)
            if result.reason == Reason.SCHEDULER_RESTART:
                log.warning(
                    "job %s: %s was running when the scheduler last stopped; outcome unknown",
                    result.job,
                    result.operation,
                )
                self.metrics.count_result(result)
                self._announce(result)
        self._build_entries(now)
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler-loop")
        catch_up = asyncio.create_task(self._catch_up(now), name="catch-up")
        self._tasks[catch_up] = ("", Operation.SNAPSHOTS)
        catch_up.add_done_callback(lambda task: self._tasks.pop(task, None))

    # ------------------------------------------------------------- scheduling

    @property
    def ready(self) -> bool:
        """True only while the loop is alive, recent and not shutting down."""
        if self.stop_event.is_set() or self._loop_task is None or self._loop_task.done():
            return False
        if self.heartbeat is None:
            return False
        age = (self.clock() - self.heartbeat).total_seconds()
        return age <= self.config.http.max_heartbeat_age.total_seconds()

    async def _loop(self) -> None:
        while not self.stop_event.is_set():
            now = self.clock()
            self.heartbeat = now
            self.metrics.heartbeat.set(now.timestamp())
            for entry in self.entries:
                if entry.next_fire <= now:
                    try:
                        self._fire(entry, now)
                    except Exception:
                        log.exception("scheduling %s for job %s failed", entry.operation, entry.job)
            upcoming = min((entry.next_fire for entry in self.entries), default=None)
            delay = TICK
            if upcoming is not None:
                delay = min(max((upcoming - self.clock()).total_seconds(), 0.05), TICK)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), delay)

    def _fire(self, entry: ScheduleEntry, now: datetime) -> None:
        """Handle every occurrence of ``entry`` that is due at ``now``."""
        due = entry.schedule.between(entry.next_fire - timedelta(microseconds=1), now)
        entry.next_fire = entry.schedule.next_after(now)
        self.metrics.next_run.labels(entry.job, entry.operation).set(entry.next_fire.timestamp())
        if not due:  # the clock moved backwards past the planned occurrence
            return
        # Several occurrences that passed together (suspend, clock jump) collapse
        # into one run; the older ones are counted as missed.
        if len(due) > 1:
            self.metrics.missed.labels(entry.job, entry.operation).inc(len(due) - 1)
        lateness = now - due[-1]
        allowed = entry.grace if entry.policy == "catch_up" else ON_TIME
        if lateness > allowed:
            self.metrics.missed.labels(entry.job, entry.operation).inc()
            self._skip(
                entry.job,
                entry.operation,
                Trigger.SCHEDULE,
                Reason.MISSED,
                f"the run scheduled for {due[-1].isoformat()} was missed by "
                f"{int(lateness.total_seconds())}s (missed_run_policy: {entry.policy})",
            )
            return
        trigger = Trigger.SCHEDULE if lateness <= ON_TIME else Trigger.CATCH_UP
        self.submit(entry.job, entry.operation, trigger)

    async def _catch_up(self, now: datetime) -> None:
        """Run once for a schedule that passed while the scheduler was down."""
        for entry in self.entries:
            if entry.policy != "catch_up" or self.stop_event.is_set():
                continue
            previous = entry.schedule.previous(now)
            if now - previous > entry.grace:
                continue
            try:
                missed = await self._was_missed(entry, previous)
            except Exception:
                log.exception("catch-up check for job %s failed", entry.job)
                continue
            if missed:
                log.info(
                    "job %s: %s scheduled for %s did not run; catching up now",
                    entry.job,
                    entry.operation,
                    previous.isoformat(),
                )
                self.metrics.missed.labels(entry.job, entry.operation).inc()
                self.submit(entry.job, entry.operation, Trigger.CATCH_UP)

    async def _was_missed(self, entry: ScheduleEntry, previous: datetime) -> bool:
        """Is there evidence that the occurrence at ``previous`` never ran?"""
        last = self.last_results.get((entry.job, entry.operation))
        if last is not None and last.started_at is not None:
            if last.started_at < previous - ON_TIME:
                return True
            # It did start. Run again only if that run never reached a result.
            return last.outcome in (Outcome.UNKNOWN, Outcome.INTERRUPTED)
        if entry.operation != Operation.BACKUP:
            return False
        # No recorded state: ask Rustic. Without an older snapshot there is no
        # evidence that this job ever ran, so nothing is considered missed.
        runtime = self.runtimes[entry.job]
        timeout = runtime.job.snapshots.timeout.total_seconds()
        try:
            async with self.locks.hold(runtime.lock_key, timeout) as fd:
                attempt = await self._snapshots(runtime, fd, None)
        except LockTimeout:
            return False
        if attempt.outcome != Outcome.SUCCEEDED:
            log.warning(
                "job %s: cannot tell whether a run was missed (%s); waiting for the next "
                "scheduled run",
                entry.job,
                attempt.detail,
            )
            return False
        latest = attempt.data.get("latest")
        return latest is not None and latest < (previous - ON_TIME).timestamp()

    # ------------------------------------------------------------- submission

    def submit(self, job: str, operation: Operation, trigger: Trigger) -> tuple[bool, str]:
        """Queue an operation. Returns whether it was accepted, and why not."""
        key = (job, operation)
        if self.stop_event.is_set():
            return False, "the scheduler is shutting down"
        if key in self._active:
            state = self._active[key]
            self._skip(
                job,
                operation,
                trigger,
                Reason.ALREADY_RUNNING,
                f"the previous {operation} of this job is still {state}",
            )
            return False, f"{operation} for job {job} is already {state}"
        self._active[key] = "queued"
        task = asyncio.create_task(
            self._pipeline(self.runtimes[job], operation, trigger), name=f"{job}/{operation}"
        )
        self._tasks[task] = key
        task.add_done_callback(lambda done: self._tasks.pop(done, None))
        return True, "queued"

    def trigger(self, job: str, operation: Operation) -> tuple[bool, str]:
        """Manual invocation. Destructive operations must be enabled in the config."""
        if job not in self.runtimes:
            return False, f"unknown job {job!r}"
        cfg = self.runtimes[job].job
        if operation == Operation.FORGET and not cfg.forget.enabled:
            return False, "forget is not enabled for this job; it will not be run on request"
        if operation == Operation.PRUNE and not cfg.prune.enabled:
            return False, "prune is not enabled for this job; it will not be run on request"
        return self.submit(job, operation, Trigger.MANUAL)

    # --------------------------------------------------------------- pipeline

    async def _pipeline(self, runtime: JobRuntime, operation: Operation, trigger: Trigger) -> None:
        key = (runtime.name, operation)
        self.metrics.queued.labels(*key).set(1)
        self._event(runtime.name, operation, "queued", {"trigger": str(trigger)})
        try:
            wait = runtime.job.repository_wait_timeout.total_seconds()
            async with self.locks.hold(runtime.lock_key, wait) as fd:
                self.metrics.queued.labels(*key).set(0)
                if self.stop_event.is_set():
                    self._skip(runtime.name, operation, trigger, Reason.SHUTDOWN, "shutting down")
                    return
                result = await self._run(runtime, operation, trigger, fd)
                if operation == Operation.BACKUP:
                    await self._after_backup(runtime, result, fd)
        except LockTimeout:
            self._skip(
                runtime.name,
                operation,
                trigger,
                Reason.REPOSITORY_BUSY,
                f"the repository stayed busy for {int(wait)}s (repository_wait_timeout)",
            )
        except asyncio.CancelledError:
            if self._active.get(key) == "queued":
                self._skip(runtime.name, operation, trigger, Reason.SHUTDOWN, "shutting down")
            raise
        except Exception:
            log.exception("job %s: %s failed inside the scheduler", runtime.name, operation)
        finally:
            self.metrics.queued.labels(*key).set(0)
            self._active.pop(key, None)

    async def _after_backup(
        self, runtime: JobRuntime, backup: OperationResult, fd: int | None
    ) -> None:
        name = runtime.name
        blocked_by_check = False
        for operation in _AFTER_BACKUP_ORDER:
            cfg = getattr(runtime.job, operation.value)
            if not (cfg.enabled and cfg.after_backup):
                continue
            key = (name, operation)
            trigger = Trigger.AFTER_BACKUP
            if self.stop_event.is_set():
                self._skip(name, operation, trigger, Reason.SHUTDOWN, "shutting down")
            elif operation != Operation.SNAPSHOTS and backup.outcome != Outcome.SUCCEEDED:
                self._skip(
                    name,
                    operation,
                    trigger,
                    Reason.BACKUP_NOT_SUCCESSFUL,
                    f"not run because the backup {backup.outcome}",
                )
            elif operation == Operation.FORGET and blocked_by_check:
                self._skip(
                    name,
                    operation,
                    trigger,
                    Reason.BACKUP_NOT_SUCCESSFUL,
                    "not run because the repository check after the backup did not succeed",
                )
            elif key in self._active:
                self._skip(
                    name, operation, trigger, Reason.ALREADY_RUNNING, "already queued separately"
                )
            else:
                self._active[key] = "running"
                try:
                    result = await self._run(runtime, operation, trigger, fd)
                finally:
                    self._active.pop(key, None)
                if operation == Operation.CHECK and result.outcome != Outcome.SUCCEEDED:
                    blocked_by_check = True

    async def _run(
        self, runtime: JobRuntime, operation: Operation, trigger: Trigger, fd: int | None
    ) -> OperationResult:
        """Run an operation, with retries, and record its single final result."""
        key = (runtime.name, operation)
        cfg = getattr(runtime.job, operation.value)
        started = self.clock()
        log_file = self.state.run_log_path(runtime.name, operation, started)
        result = OperationResult(
            job=runtime.name,
            operation=operation,
            trigger=trigger,
            outcome=Outcome.UNKNOWN,
            started_at=started,
            log_file=str(log_file) if log_file else None,
        )
        self._active[key] = "running"
        self.metrics.running.labels(*key).set(1)
        self.metrics.clear_progress(*key)
        self.progress.pop(key, None)
        self.state.mark_running(result)
        self._event(runtime.name, operation, "started", {"trigger": str(trigger)})
        log.info("job %s: %s started (%s)", runtime.name, operation, trigger)
        attempt = Attempt(Outcome.UNKNOWN, detail="the operation did not run to a result")
        try:
            attempts = cfg.retries + 1
            for number in range(1, attempts + 1):
                result.attempts = number
                attempt = await self._execute(runtime, operation, fd, log_file)
                retry = (
                    attempt.outcome == Outcome.FAILED
                    and attempt.reason not in _NO_RETRY
                    and number < attempts
                    and not self.stop_event.is_set()
                )
                if not retry:
                    break
                log.warning(
                    "job %s: %s failed (attempt %d of %d): %s; retrying in %s",
                    runtime.name,
                    operation,
                    number,
                    attempts,
                    attempt.detail,
                    cfg.retry_delay,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), cfg.retry_delay.total_seconds())
                if self.stop_event.is_set():
                    break
        except asyncio.CancelledError:
            attempt = Attempt(Outcome.INTERRUPTED, Reason.SHUTDOWN, detail="cancelled")
            raise
        except Exception as exc:
            log.exception("job %s: %s raised inside the scheduler", runtime.name, operation)
            attempt = Attempt(
                Outcome.UNKNOWN, detail=f"internal scheduler error: {type(exc).__name__}"
            )
        finally:
            result.outcome = attempt.outcome
            result.reason = attempt.reason
            result.exit_code = attempt.exit_code
            result.detail = REDACTOR(attempt.detail)
            result.dry_run = attempt.dry_run
            result.data = attempt.data
            result.finished_at = self.clock()
            self._finish(result)
        return result

    # -------------------------------------------------------------- execution

    async def _process(
        self,
        runtime: JobRuntime,
        argv: list[str],
        timeout: timedelta,
        fd: int | None,
        log_file: Any,
        *,
        mode: str,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> ProcessResult:
        return await run_process(
            argv,
            runtime.env,
            timeout=timeout.total_seconds(),
            stop=self.stop_event,
            stop_grace=self.config.scheduler.stop_grace.total_seconds(),
            mode=mode,
            on_message=on_message,
            log_file=log_file,
            pass_fds=(fd,) if fd is not None else (),
            name=f"rustic[{runtime.name}]",
        )

    async def _execute(
        self, runtime: JobRuntime, operation: Operation, fd: int | None, log_file: Any
    ) -> Attempt:
        match operation:
            case Operation.BACKUP:
                return await self._backup(runtime, fd, log_file)
            case Operation.SNAPSHOTS:
                return await self._snapshots(runtime, fd, log_file)
            case Operation.FORGET:
                return await self._forget(runtime, fd, log_file)
            case Operation.PRUNE:
                return await self._plain(runtime, Operation.PRUNE, fd, log_file)
            case Operation.CHECK:
                return await self._plain(runtime, Operation.CHECK, fd, log_file)

    def _on_message(
        self, runtime: JobRuntime, operation: Operation
    ) -> Callable[[dict[str, Any]], None]:
        key = (runtime.name, operation)

        def handle(message: dict[str, Any]) -> None:
            if message.get("message_type") != "status":
                return
            now = self.clock()
            progress = progress_from(message, now)
            self.progress[key] = progress
            self.metrics.set_progress(runtime.name, operation, progress)
            if self.mqtt is None:
                return
            last = self._progress_sent.get(key)
            if last is None or now - last >= self.config.mqtt.progress_interval:
                self._progress_sent[key] = now
                self.mqtt.publish(
                    self.mqtt.topic("jobs", runtime.name, operation, "progress"),
                    progress.to_dict(),
                    droppable=True,
                )

        return handle

    async def _backup(self, runtime: JobRuntime, fd: int | None, log_file: Any) -> Attempt:
        since = self.clock()
        process = await self._process(
            runtime,
            commands.backup_command(self.config, runtime),
            runtime.job.backup.timeout,
            fd,
            log_file,
            mode="ndjson",
            on_message=self._on_message(runtime, Operation.BACKUP),
        )
        problem = classify(process)
        if problem is not None:
            return problem
        data = summarise_backup(process.messages)
        if data is None:
            # Exit status 0 without a summary is what an interrupted Rustic looks like.
            return Attempt(
                Outcome.UNKNOWN,
                Reason.NO_SUMMARY,
                process.exit_code,
                "Rustic exited with status 0 but reported no backup summary",
            )
        detail = ""
        if data["snapshots_saved"] == 0:
            detail = "no snapshot was saved (unchanged source with skip-if-unchanged, or a dry run)"
        await self._verify_push(runtime, since, data)
        return Attempt(Outcome.SUCCEEDED, exit_code=process.exit_code, detail=detail, data=data)

    async def _verify_push(
        self, runtime: JobRuntime, since: datetime, data: dict[str, Any]
    ) -> None:
        """Record whether Rustic's own metrics arrived. Never affects the backup."""
        if self._pushgateway is None or runtime.pushgateway_url != self.config.pushgateway.url:
            return
        try:
            verification = await self._pushgateway.verify(
                runtime.label,
                runtime.job.host,
                since,
                cleanup_stale=self.config.pushgateway.cleanup_stale_groups,
            )
        except Exception as exc:
            log.exception("job %s: Pushgateway verification failed", runtime.name)
            data["pushgateway"] = {"result": "unreachable", "detail": type(exc).__name__}
            self.metrics.pushgateway_checks.labels(runtime.name, "unreachable").inc()
            self.metrics.pushgateway_verified.labels(runtime.name).set(0)
            return
        delivered = verification.result == "delivered"
        self.metrics.pushgateway_checks.labels(runtime.name, verification.result).inc()
        self.metrics.pushgateway_verified.labels(runtime.name).set(1 if delivered else 0)
        if verification.push_time is not None:
            self.metrics.pushgateway_last_push.labels(runtime.name).set(verification.push_time)
        data["pushgateway"] = {"result": verification.result, "detail": verification.detail}
        if not delivered:
            log.warning(
                "job %s: Rustic's metrics did not reach the Pushgateway (%s %s)",
                runtime.name,
                verification.result,
                verification.detail,
            )

    async def _snapshots(self, runtime: JobRuntime, fd: int | None, log_file: Any) -> Attempt:
        process = await self._process(
            runtime,
            commands.snapshots_command(runtime),
            runtime.job.snapshots.timeout,
            fd,
            log_file,
            mode="capture",
        )
        problem = classify(process)
        if problem is not None:
            return problem
        try:
            data = summarise_snapshots(json.loads(process.stdout), runtime)
        except LookupError as exc:
            return Attempt(Outcome.FAILED, Reason.SCOPE_VIOLATION, process.exit_code, str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            return Attempt(
                Outcome.FAILED, Reason.BAD_OUTPUT, process.exit_code, f"unreadable output: {exc}"
            )
        return Attempt(Outcome.SUCCEEDED, exit_code=process.exit_code, data=data)

    async def _forget(self, runtime: JobRuntime, fd: int | None, log_file: Any) -> Attempt:
        """Preview, verify the scope and limits, and only then remove snapshots."""
        forget = runtime.job.forget
        preview = await self._process(
            runtime,
            commands.forget_command(runtime, dry_run=True),
            forget.timeout,
            fd,
            log_file,
            mode="capture",
        )
        problem = classify(preview)
        if problem is not None:
            return problem
        try:
            plan = summarise_forget(json.loads(preview.stdout), runtime)
        except LookupError as exc:
            return Attempt(
                Outcome.FAILED,
                Reason.SCOPE_VIOLATION,
                preview.exit_code,
                f"{exc}; nothing was removed",
            )
        except (ValueError, KeyError, TypeError) as exc:
            return Attempt(
                Outcome.FAILED,
                Reason.BAD_OUTPUT,
                preview.exit_code,
                f"unreadable preview ({exc}); nothing was removed",
            )
        if forget.max_remove is not None and plan["removed"] > forget.max_remove:
            return Attempt(
                Outcome.FAILED,
                Reason.REMOVAL_LIMIT,
                preview.exit_code,
                f"{plan['removed']} snapshots would be removed, more than max_remove "
                f"({forget.max_remove}); nothing was removed",
                data=plan,
            )
        if plan["removed"] and plan["kept"] == 0 and forget.keep.get("keep-none") is not True:
            return Attempt(
                Outcome.FAILED,
                Reason.REMOVAL_LIMIT,
                preview.exit_code,
                "every snapshot of the job would be removed; nothing was removed",
                data=plan,
            )
        if forget.dry_run:
            return Attempt(Outcome.SUCCEEDED, exit_code=preview.exit_code, dry_run=True, data=plan)
        if plan["removed"] == 0 and not forget.prune:
            return Attempt(Outcome.SUCCEEDED, exit_code=preview.exit_code, data=plan)
        real = await self._process(
            runtime,
            commands.forget_command(runtime, dry_run=False),
            forget.timeout,
            fd,
            log_file,
            mode="capture",
        )
        problem = classify(real)
        if problem is not None:
            problem.detail += " (some snapshots may already have been removed)"
            return problem
        plan["pruned"] = forget.prune
        return Attempt(Outcome.SUCCEEDED, exit_code=real.exit_code, data=plan)

    async def _plain(
        self, runtime: JobRuntime, operation: Operation, fd: int | None, log_file: Any
    ) -> Attempt:
        """Check and prune: the result is Rustic's exit status, minus interruptions."""
        cfg = getattr(runtime.job, operation.value)
        process = await self._process(
            runtime,
            commands.command_for(self.config, runtime, operation),
            cfg.timeout,
            fd,
            log_file,
            mode="ndjson",
            on_message=self._on_message(runtime, operation),
        )
        problem = classify(process)
        if problem is not None:
            return problem
        return Attempt(
            Outcome.SUCCEEDED,
            exit_code=process.exit_code,
            dry_run=bool(getattr(cfg, "dry_run", False)),
        )

    # ---------------------------------------------------------------- results

    def _finish(self, result: OperationResult) -> None:
        key = (result.job, result.operation)
        self.last_results[key] = result
        self.last_skips.pop(key, None)
        self.progress.pop(key, None)
        self._progress_sent.pop(key, None)
        self.metrics.running.labels(*key).set(0)
        self.metrics.clear_progress(*key)
        self.metrics.count_result(result)
        self.metrics.set_last_result(result)
        errors = self.state.write_errors
        self.state.record(result)
        self.state.prune_run_logs(result.job, result.operation)
        if self.state.write_errors > errors:
            self.metrics.state_write_errors.inc(self.state.write_errors - errors)
        level = logging.INFO if result.outcome == Outcome.SUCCEEDED else logging.ERROR
        log.log(
            level,
            "job %s: %s %s%s in %.1fs%s",
            result.job,
            result.operation,
            result.outcome,
            f" ({result.reason})" if result.reason != Reason.NONE else "",
            result.duration_seconds or 0.0,
            f": {result.detail}" if result.detail else "",
        )
        self._announce(result)

    def _skip(
        self, job: str, operation: Operation, trigger: Trigger, reason: Reason, detail: str
    ) -> None:
        result = OperationResult(
            job=job,
            operation=operation,
            trigger=trigger,
            outcome=Outcome.SKIPPED,
            reason=reason,
            finished_at=self.clock(),
            detail=detail,
        )
        self.last_skips[(job, operation)] = result
        self.metrics.count_result(result)
        # A skip does not replace the outcome of a run that is still going on.
        if self._active.get((job, operation)) != "running":
            self.metrics.set_last_result(result)
        log.warning("job %s: %s skipped (%s): %s", job, operation, reason, detail)
        self._announce(result)

    def _announce(self, result: OperationResult) -> None:
        """Hand a result to MQTT and notifications; neither can fail the caller."""
        try:
            if self.mqtt is not None:
                event = "skipped" if result.outcome == Outcome.SKIPPED else "finished"
                payload = result.to_dict()
                self._event(result.job, result.operation, event, payload)
                if result.outcome != Outcome.SKIPPED:
                    self.mqtt.publish(
                        self.mqtt.topic("jobs", result.job, result.operation, "result"),
                        payload,
                        retain=self.config.mqtt.retain_results,
                    )
            if self.notifier is not None:
                self.notifier.submit(result)
        except Exception:
            log.exception("reporting a result failed")

    def _event(self, job: str, operation: Operation, event: str, payload: dict[str, Any]) -> None:
        if self.mqtt is None:
            return
        self.mqtt.publish(
            self.mqtt.topic("jobs", job, operation, "event"),
            {
                "event": event,
                "job": job,
                "operation": str(operation),
                "at": self.clock().isoformat(),
            }
            | payload,
        )

    # --------------------------------------------------------------- shutdown

    async def shutdown(self) -> None:
        """Stop scheduling, stop Rustic gracefully and wait for results to be recorded."""
        self.stop_event.set()
        for task, key in list(self._tasks.items()):
            if self._active.get(key) == "queued" or key[0] == "":
                task.cancel()
        if self._loop_task is not None:
            await asyncio.gather(self._loop_task, return_exceptions=True)
        if self._tasks:
            limit = self.config.scheduler.stop_grace.total_seconds() + 30
            _, pending = await asyncio.wait(list(self._tasks), timeout=limit)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait until nothing is queued or running (used by tests and `--wait`)."""
        while self._tasks:
            await asyncio.wait(list(self._tasks))

    # ----------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        jobs: dict[str, Any] = {}
        for name, runtime in self.runtimes.items():
            operations: dict[str, Any] = {}
            for operation in Operation:
                key = (name, operation)
                last = self.last_results.get(key)
                skip = self.last_skips.get(key)
                progress = self.progress.get(key)
                entry = next(
                    (e for e in self.entries if e.job == name and e.operation == operation), None
                )
                if not (
                    last or skip or entry or key in self._active or operation == Operation.BACKUP
                ):
                    continue
                operations[str(operation)] = {
                    "state": self._active.get(key, "idle"),
                    "next_run": entry.next_fire.isoformat() if entry else None,
                    "schedule": entry.schedule.describe() if entry else None,
                    "progress": progress.to_dict() if progress else None,
                    "last_result": last.to_dict() if last else None,
                    "last_skip": skip.to_dict() if skip else None,
                }
            jobs[name] = {
                "label": runtime.label,
                "host": runtime.job.host,
                "repository_lock": runtime.lock_key,
                "repository_busy": self.locks.is_busy(runtime.lock_key),
                "operations": operations,
            }
        return {
            "ready": self.ready,
            "shutting_down": self.stop_event.is_set(),
            "started_at": self.started_at.isoformat(),
            "heartbeat": self.heartbeat.isoformat() if self.heartbeat else None,
            "state_dir_usable": self.state.usable,
            "jobs": jobs,
        }
