"""Runs one Rustic process and reports what happened, without interpreting it.

Verified against Rustic 0.11.4:

* stdout carries newline-delimited JSON when `--json-progress` is set
  (`message_type` is `status` or `summary`); logs go to stderr.
* On SIGINT or SIGTERM Rustic logs "Ctrl-C received, shutting down..." and exits
  with status **0**. The exit status alone therefore never proves success.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .redact import REDACTOR

log = logging.getLogger(__name__)

INTERRUPT_MARKER = "Ctrl-C received"
NON_TERMINAL_ERROR = "error: IO error: not a terminal"
NON_TERMINAL_HINT = (
    "[WARN] Rustic or a hook may have tried to prompt without a terminal. "
    "Configure a repository password using [repository] password-file or password-command, "
    "or make hooks non-interactive."
)
_TAIL_LINES = 40
_MAX_CAPTURE = 512 * 1024 * 1024
#: Longer output lines are shortened in logs.
_MAX_LOG_LINE = 4000


@dataclass
class ProcessResult:
    exit_code: int | None = None
    spawn_error: str | None = None
    timed_out: bool = False
    #: The scheduler asked the process to stop (shutdown or cancellation).
    stopped_by_scheduler: bool = False
    #: Rustic reported that it received a termination signal.
    saw_interrupt: bool = False
    #: Rustic reported a non-interactive terminal error.
    saw_non_terminal_error: bool = False
    #: Parsed JSON objects from stdout, in order (only in `ndjson` mode).
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Raw stdout (only in `capture` mode).
    stdout: bytes = b""
    stdout_truncated: bool = False
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def tail(self) -> str:
        return "\n".join(self.stderr_tail)


async def _lines(stream: asyncio.StreamReader) -> Any:
    """Yield decoded lines without asyncio's per-line size limit."""
    buffer = b""
    while chunk := await stream.read(65536):
        buffer += chunk
        *complete, buffer = buffer.split(b"\n")
        for raw in complete:
            yield raw.decode(errors="replace").rstrip("\r")
    if buffer:
        yield buffer.decode(errors="replace").rstrip("\r")


def _terminate(process: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


async def run_process(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout: float | None,
    stop: asyncio.Event,
    stop_grace: float,
    mode: str = "ndjson",
    on_message: Callable[[dict[str, Any]], None] | None = None,
    log_file: Path | None = None,
    pass_fds: tuple[int, ...] = (),
    name: str = "rustic",
) -> ProcessResult:
    """Run ``argv`` to completion.

    ``stop`` being set, or ``timeout`` expiring, sends SIGTERM to the process
    group, then SIGKILL after ``stop_grace`` seconds.
    """
    result = ProcessResult()
    sink: IO[str] | None = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            sink = log_file.open("w", encoding="utf-8")
            os.chmod(log_file, 0o600)
        except OSError as exc:
            log.warning("cannot write run log %s: %s", log_file, exc.strerror or exc)
    tail: deque[str] = deque(maxlen=_TAIL_LINES)

    def record(line: str, stream: str) -> None:
        if len(line) > _MAX_LOG_LINE:
            line = f"{line[:_MAX_LOG_LINE]} ... [{len(line) - _MAX_LOG_LINE} more characters]"
        clean = REDACTOR(line)
        if sink is not None:
            sink.write(f"{clean}\n")
            sink.flush()
        if stream == "stderr":
            tail.append(clean)
            if INTERRUPT_MARKER in line:
                result.saw_interrupt = True
            level = (
                logging.ERROR
                if "[ERROR]" in line or "error:" in line
                else logging.WARNING
                if "[WARN]" in line
                else logging.DEBUG
            )
            log.log(level, "%s: %s", name, clean)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # Own process group: terminal signals do not reach Rustic directly,
            # and the scheduler can stop Rustic together with its hook commands.
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except OSError as exc:
        result.spawn_error = f"{argv[0]}: {exc.strerror or exc}"
        if sink is not None:
            sink.write(f"could not start: {result.spawn_error}\n")
            sink.close()
        return result

    async def read_stdout() -> None:
        assert process.stdout is not None
        if mode == "capture":
            chunks: list[bytes] = []
            size = 0
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size <= _MAX_CAPTURE:
                    chunks.append(chunk)
                else:
                    result.stdout_truncated = True
            result.stdout = b"".join(chunks)
            return
        async for line in _lines(process.stdout):
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                record(line, "stdout")
                continue
            if not isinstance(message, dict):
                record(line, "stdout")
                continue
            result.messages.append(message)
            if message.get("message_type") != "status":
                record(line, "stdout")
            if on_message is not None:
                try:
                    on_message(message)
                except Exception:  # a reporting bug must never break a backup
                    log.exception("progress handler failed")

    async def read_stderr() -> None:
        assert process.stderr is not None
        async for line in _lines(process.stderr):
            record(line, "stderr")
            if not result.saw_non_terminal_error and line.strip() == NON_TERMINAL_ERROR:
                result.saw_non_terminal_error = True
                record(NON_TERMINAL_HINT, "stderr")

    readers = asyncio.gather(read_stdout(), read_stderr())
    waiter = asyncio.ensure_future(process.wait())
    stopper = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter not in done:
            if stopper in done:
                result.stopped_by_scheduler = True
            else:
                result.timed_out = True
            _terminate(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), stop_grace)
            except TimeoutError:
                log.warning("%s did not stop within %.0fs; killing it", name, stop_grace)
                _terminate(process, signal.SIGKILL)
                await waiter
    except asyncio.CancelledError:
        # Never leave a Rustic process behind when the caller is cancelled.
        _terminate(process, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.shield(waiter)
        raise
    finally:
        stopper.cancel()
        try:
            # Hook commands may keep the pipes open; do not wait for them forever.
            await asyncio.wait_for(readers, 10)
        except (TimeoutError, asyncio.CancelledError):
            readers.cancel()
        except Exception:
            log.exception("reading output of %s failed", name)
        if sink is not None:
            sink.close()
    result.exit_code = process.returncode
    result.stderr_tail = list(tail)
    return result
