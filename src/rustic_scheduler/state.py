"""Small on-disk state: last results and run logs.

This is not a database. It is one JSON file with the last result of every
operation, so that a restart does not forget them, plus a marker for runs in
flight so that a crash can be reported as an unknown outcome. Long-term history
lives in Rustic's snapshots and in Prometheus. Without a writable state
directory the scheduler still works; it just starts with no memory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import Operation, OperationResult, Outcome, Reason, Trigger, utcnow

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, state_dir: Path | None, logs_to_keep: int = 20) -> None:
        self.state_dir = state_dir
        self.logs_to_keep = logs_to_keep
        self.write_errors = 0
        self._results: dict[str, dict[str, Any]] = {}
        self._running: dict[str, dict[str, Any]] = {}
        self.usable = False
        if state_dir is not None:
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                self.usable = os.access(state_dir, os.W_OK)
            except OSError:
                self.usable = False
            if not self.usable:
                log.warning("state directory %s is not writable; running without state", state_dir)

    @property
    def lock_dir(self) -> Path | None:
        return self.state_dir / "locks" if self.usable and self.state_dir else None

    @property
    def control_socket(self) -> Path | None:
        return self.state_dir / "control.sock" if self.usable and self.state_dir else None

    @staticmethod
    def _key(job: str, operation: Operation) -> str:
        return f"{job}/{operation}"

    def load(self) -> list[OperationResult]:
        """Read the previous state; runs that were in flight become ``unknown``."""
        if not self.usable or self.state_dir is None:
            return []
        path = self.state_dir / "state.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", path, exc)
            return []
        results: list[OperationResult] = []
        for key, item in (raw.get("results") or {}).items():
            try:
                results.append(OperationResult.from_dict(item))
                self._results[key] = item
            except (KeyError, ValueError, TypeError):
                log.warning("ignoring malformed state entry %s", key)
        for key, item in (raw.get("running") or {}).items():
            try:
                result = OperationResult(
                    job=item["job"],
                    operation=Operation(item["operation"]),
                    trigger=Trigger(item["trigger"]),
                    outcome=Outcome.UNKNOWN,
                    reason=Reason.SCHEDULER_RESTART,
                    started_at=datetime.fromisoformat(item["started_at"]),
                    finished_at=utcnow(),
                    detail="the scheduler stopped while this operation was running; "
                    "its result was never observed",
                    log_file=item.get("log_file"),
                )
            except (KeyError, ValueError, TypeError):
                continue
            results = [r for r in results if self._key(r.job, r.operation) != key]
            results.append(result)
            self._results[key] = result.to_dict()
        self._write()
        return results

    def mark_running(self, result: OperationResult) -> None:
        self._running[self._key(result.job, result.operation)] = {
            "job": result.job,
            "operation": str(result.operation),
            "trigger": str(result.trigger),
            "started_at": (result.started_at or utcnow()).isoformat(),
            "log_file": result.log_file,
        }
        self._write()

    def record(self, result: OperationResult) -> None:
        key = self._key(result.job, result.operation)
        self._running.pop(key, None)
        self._results[key] = result.to_dict()
        self._write()

    def _write(self) -> None:
        if not self.usable or self.state_dir is None:
            return
        payload = {"version": 1, "results": self._results, "running": self._running}
        try:
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_dir / "state.json")
        except OSError as exc:
            self.write_errors += 1
            log.warning("cannot write state file: %s", exc.strerror or exc)

    def run_log_path(self, job: str, operation: Operation, started: datetime) -> Path | None:
        if not self.usable or self.state_dir is None or self.logs_to_keep == 0:
            return None
        stamp = started.strftime("%Y%m%dT%H%M%S.%fZ")
        return self.state_dir / "logs" / job / f"{stamp}-{operation}.log"

    def prune_run_logs(self, job: str, operation: Operation) -> None:
        if not self.usable or self.state_dir is None:
            return
        directory = self.state_dir / "logs" / job
        with contextlib.suppress(OSError):
            logs = sorted(directory.glob(f"*-{operation}.log"))
            for old in logs[: max(len(logs) - self.logs_to_keep, 0)]:
                old.unlink(missing_ok=True)
