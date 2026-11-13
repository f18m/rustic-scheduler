"""Outcome vocabulary shared by the engine, metrics, MQTT and notifications."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Operation(StrEnum):
    BACKUP = "backup"
    FORGET = "forget"
    PRUNE = "prune"
    CHECK = "check"
    SNAPSHOTS = "snapshots"


class Outcome(StrEnum):
    """Final state of one operation run.

    ``succeeded``    Rustic finished and its result was confirmed.
    ``failed``       Rustic reported an error, timed out, or could not be started.
    ``skipped``      The run was never started (overlap, busy repository, a failed
                     prerequisite, a missed schedule).
    ``interrupted``  The run was started and then stopped by a signal or shutdown.
    ``unknown``      The run was started but its result cannot be established.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    CATCH_UP = "catch_up"
    MANUAL = "manual"
    MQTT = "mqtt"
    WEB = "web"
    AFTER_BACKUP = "after_backup"


class Reason(StrEnum):
    """Bounded set of explanations, safe to use as a metric label."""

    NONE = "none"
    EXIT_CODE = "exit_code"
    KILLED = "killed"
    TIMEOUT = "timeout"
    NO_SUMMARY = "no_summary"
    BAD_OUTPUT = "bad_output"
    SPAWN_ERROR = "spawn_error"
    SCOPE_VIOLATION = "scope_violation"
    REMOVAL_LIMIT = "removal_limit"
    SHUTDOWN = "shutdown"
    SIGNAL = "signal"
    ALREADY_RUNNING = "already_running"
    REPOSITORY_BUSY = "repository_busy"
    BACKUP_NOT_SUCCESSFUL = "backup_not_successful"
    MISSED = "missed"
    SCHEDULER_RESTART = "scheduler_restart"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class OperationResult:
    job: str
    operation: Operation
    trigger: Trigger
    outcome: Outcome
    reason: Reason = Reason.NONE
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    attempts: int = 0
    dry_run: bool = False
    #: Free-form diagnostics. Never used as a metric label.
    detail: str = ""
    log_file: str | None = None
    #: Operation-specific numbers (backup summary, forget counts, ...).
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max((self.finished_at - self.started_at).total_seconds(), 0.0)

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        for key in ("operation", "trigger", "outcome", "reason"):
            raw[key] = str(raw[key])
        for key in ("started_at", "finished_at"):
            raw[key] = raw[key].isoformat() if raw[key] else None
        raw["duration_seconds"] = self.duration_seconds
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OperationResult:
        def when(value: Any) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            job=raw["job"],
            operation=Operation(raw["operation"]),
            trigger=Trigger(raw["trigger"]),
            outcome=Outcome(raw["outcome"]),
            reason=Reason(raw.get("reason", "none")),
            started_at=when(raw.get("started_at")),
            finished_at=when(raw.get("finished_at")),
            exit_code=raw.get("exit_code"),
            attempts=raw.get("attempts", 0),
            dry_run=raw.get("dry_run", False),
            detail=raw.get("detail", ""),
            log_file=raw.get("log_file"),
            data=raw.get("data", {}),
        )


@dataclass
class Progress:
    """Latest progress message of a running operation. ``None`` means unknown."""

    updated_at: datetime
    elapsed_seconds: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    ratio: float | None = None
    eta_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["updated_at"] = self.updated_at.isoformat()
        return raw
