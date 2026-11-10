"""Repository-level mutual exclusion.

Two layers are combined:

* an ``asyncio.Lock`` per repository, which queues operations inside the
  scheduler process in FIFO order, and
* an advisory ``flock`` on a file in the state directory, which also covers the
  ``rustic-scheduler rustic`` helper and a second scheduler sharing the volume.

The lock file descriptor is handed to the Rustic child process, so if the
scheduler dies the repository stays locked for as long as that orphan runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

log = logging.getLogger(__name__)


class LockTimeout(Exception):
    """The repository stayed busy for longer than the caller wanted to wait."""


class RepositoryLocks:
    def __init__(self, lock_dir: Path | None, poll_interval: float = 1.0) -> None:
        self._lock_dir = lock_dir
        self._poll = poll_interval
        self._locks: dict[str, asyncio.Lock] = {}
        if lock_dir is not None:
            lock_dir.mkdir(parents=True, exist_ok=True)

    def is_busy(self, key: str) -> bool:
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    @contextlib.asynccontextmanager
    async def hold(self, key: str, timeout: float | None) -> AsyncIterator[int | None]:
        """Hold the repository lock; yields the lock file descriptor, if any."""
        deadline = None if timeout is None else time.monotonic() + timeout
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout)
        except TimeoutError as exc:
            raise LockTimeout(key) from exc
        fd: int | None = None
        try:
            fd = await self._flock(key, deadline)
            yield fd
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            lock.release()

    async def _flock(self, key: str, deadline: float | None) -> int | None:
        if self._lock_dir is None:
            return None
        fd = os.open(self._lock_dir / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeout(key) from None
                    await asyncio.sleep(self._poll)
        except BaseException:
            os.close(fd)
            raise
