"""A run's output for the web interface (``web.run_logs``). Read-only.

The output is the run log that ``runner.run_process`` already writes, redacted, to
``state_dir/logs/<job>/``. The file is found from the engine's own record of the run
(running, recent history or last result), never from a name or path in the request,
so only the logs of the scheduler's own runs can be read. Nothing is stored for this.

The page asks for the log from a byte offset and gets whole lines back, at most
``MAX_CHUNK`` bytes at a time. The first request of a long log starts near its end.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import WebConfig
from .engine import Engine
from .model import Operation, OperationResult

MAX_CHUNK = 256 * 1024
#: Terminal escape sequences (colours, cursor movement) and other control characters.
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class RunLogError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Chunk:
    offset: int
    next_offset: int
    text: str
    #: More of the file is already there: ask again at once.
    more: bool
    #: Bytes at the start that were left out (a long log opened at its end).
    skipped: int


def unavailable(engine: Engine, config: WebConfig) -> str | None:
    """Why no run output can be shown at all, or None when it can."""
    if not config.run_logs.enabled:
        return "showing run output is off (web.run_logs.enabled)"
    if not engine.state.usable:
        return "run logs need a writable scheduler.state_dir"
    if engine.state.logs_to_keep == 0:
        return "run logs are off (scheduler.run_logs_per_operation is 0)"
    return None


def clean(text: str) -> str:
    return _CONTROL.sub("", _ESCAPES.sub("", text))


def read_chunk(path: Path, offset: int, complete: bool) -> Chunk:
    """Whole lines of ``path`` from ``offset``. ``complete``: nothing more will be written."""
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = min(offset, size)
        skipped = 0
        if start == 0 and size > MAX_CHUNK:
            start = skipped = size - MAX_CHUNK
        handle.seek(start)
        data = handle.read(min(size - start, MAX_CHUNK))
    if skipped:
        # Begin at a line boundary.
        cut = data.find(b"\n")
        if cut >= 0:
            data = data[cut + 1 :]
            start += cut + 1
            skipped = start
    more = start + len(data) < size
    if data and not data.endswith(b"\n") and not (complete and not more):
        # A line still being written waits for the next request, unless it alone
        # fills a whole chunk.
        cut = data.rfind(b"\n")
        if cut >= 0:
            data = data[: cut + 1]
        elif len(data) < MAX_CHUNK:
            data = b""
    return Chunk(
        offset=start,
        next_offset=start + len(data),
        text=clean(data.decode(errors="replace")),
        more=more,
        skipped=skipped,
    )


def find(
    engine: Engine, job: str, operation: Operation, run_id: str | None
) -> tuple[str, OperationResult | None]:
    """The run asked for and its state: ``running``, ``finished``, ``queued`` or ``none``.

    Without ``run_id``: the running run, else the latest finished one.
    """
    key = (job, operation)
    current = engine.current.get(key)
    if run_id is None:
        if current is not None:
            return "running", current
        if engine.is_active(job, operation) == "queued":
            return "queued", None
        last = engine.last_results.get(key)
        return ("finished", last) if last is not None else ("none", None)
    if current is not None and current.run_id == run_id:
        return "running", current
    for result in (*reversed(engine.history), engine.last_results.get(key)):
        if (
            result is not None
            and result.job == job
            and result.operation == operation
            and result.run_id == run_id
        ):
            return "finished", result
    raise RunLogError(404, "this run is no longer in the scheduler's memory")


def log_of(
    engine: Engine, config: WebConfig, result: OperationResult | None, state: str, offset: int
) -> dict[str, Any]:
    """The ``log`` part of a response. Blocking: run it in a thread."""
    reason = unavailable(engine, config)
    if result is None:
        return {"available": False, "message": reason}
    if reason is None and result.log_file is None:
        reason = "no run log was written for this run"
    if reason is not None:
        return {"available": False, "message": reason}
    assert result.log_file is not None
    try:
        chunk = read_chunk(Path(result.log_file), offset, complete=state == "finished")
    except FileNotFoundError:
        if state == "running":
            # Not created yet, or it could not be written (logged by the runner).
            chunk = Chunk(0, 0, "", more=False, skipped=0)
        else:
            return {
                "available": False,
                "message": "this run's log is no longer kept (scheduler.run_logs_per_operation)",
            }
    except OSError as exc:
        return {
            "available": False,
            "message": f"this run's log cannot be read: {exc.strerror or exc}",
        }
    return {
        "available": True,
        "message": None,
        "offset": chunk.offset,
        "next_offset": chunk.next_offset,
        "text": chunk.text,
        "more": chunk.more,
        "skipped": chunk.skipped,
    }
