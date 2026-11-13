"""Read-only snapshot and file listings for the web interface (``web.browse``).

Two Rustic commands are used, both verified against Rustic 0.11.4:

* ``rustic snapshots --json --filter-label ... [--filter-host ...]``: the job's snapshots;
* ``rustic cat tree SNAPSHOT:PATH``: one directory of a snapshot as
  ``{"nodes": [{"name", "type", "size", "mtime", "mode", "uid", "user", ...}]}``.
  ``rustic ls --json`` prints names only, which is why it is not used.

A listing is one directory at a time, only of a snapshot that belongs to the job, and
takes the repository lock like every other Rustic process the scheduler starts. It does
not queue behind a run: when the repository is busy the request is refused, or answered
from the lists kept in memory here (a snapshot never changes, so a directory that was
listed once stays valid). Nothing is written to disk.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import Any

from . import commands
from .config import WebBrowseConfig
from .engine import Engine, _in_scope, classify, parse_time
from .locks import LockTimeout
from .redact import REDACTOR
from .runner import ProcessResult

log = logging.getLogger(__name__)

#: Seconds a listing waits for the repository lock before it is refused as "busy".
LOCK_WAIT = 2.0
#: Directory listings kept in memory, most recently used first out last.
_CACHED_DIRECTORIES = 32
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
_BUSY = "the repository is in use by a running operation; try again when it has finished"


class BrowseError(Exception):
    """A listing that cannot be given; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    summary = raw.get("summary") or {}
    return {
        "id": raw["id"],
        "time": raw["time"],
        "hostname": raw.get("hostname"),
        "paths": list(raw.get("paths") or []),
        "tags": list(raw.get("tags") or []),
        # Absent when Rustic did not report it: unknown is not zero.
        "files": summary.get("total_files_processed"),
        "bytes": summary.get("total_bytes_processed"),
        "bytes_added": summary.get("data_added_packed"),
    }


def _entry(node: dict[str, Any]) -> dict[str, Any]:
    kind = node.get("type")
    mode = node.get("mode")
    return {
        "name": node["name"],
        "type": kind,
        # Rustic records a size for files; for anything else it is not a content size.
        "size": node.get("size") if kind == "file" else None,
        "modified": node.get("mtime"),
        "changed": node.get("ctime"),
        # The low bits are the Unix permissions; the rest encodes the type.
        "permissions": mode & 0o7777 if isinstance(mode, int) else None,
        "user": node.get("user"),
        "group": node.get("group"),
        "uid": node.get("uid"),
        "gid": node.get("gid"),
        "link_target": node.get("linktarget"),
    }


def _rustic_message(process: ProcessResult) -> str | None:
    """The line after "Message:" in Rustic 0.11.4's error report, which says what is wrong.

    (The line starting with "error:" only names the kind of error.)
    """
    lines = [line.strip() for line in process.stderr_tail]
    if process.exit_code and "Message:" in lines[:-1]:
        return lines[lines.index("Message:") + 1][:600] or None
    return None


def normalise(path: str) -> str:
    """An absolute path inside a snapshot, without empty, `.` or `..` components."""
    if "\x00" in path:
        raise BrowseError(400, "invalid path")
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise BrowseError(400, "the path must not contain '.' or '..'")
    return "/" + "/".join(parts)


class Browser:
    def __init__(self, engine: Engine, config: WebBrowseConfig) -> None:
        self.engine = engine
        self.config = config
        #: job -> (newest history entry when listed, payload)
        self._snapshots: dict[str, tuple[object, dict[str, Any]]] = {}
        self._directories: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()

    def _marker(self) -> object:
        """Changes whenever an operation finishes, which is when a list may be out of date."""
        return self.engine.history[-1] if self.engine.history else None

    async def _rustic(self, job: str, argv: list[str], what: str) -> bytes:
        if self.engine.stop_event.is_set():
            raise BrowseError(503, "the scheduler is shutting down")
        process = await self.engine.query(job, argv, self.config.timeout, LOCK_WAIT)
        problem = classify(process)
        if problem is None and process.stdout_truncated:
            raise BrowseError(502, f"{what}: Rustic's output is too large")
        if problem is not None:
            detail = REDACTOR(_rustic_message(process) or problem.detail)
            log.warning("job %s: web interface could not list %s: %s", job, what, detail)
            missing = "not found in tree" in detail or "is not a directory" in detail
            raise BrowseError(404 if missing else 502, detail)
        return process.stdout

    async def snapshots(self, job: str, *, refresh: bool = False) -> dict[str, Any]:
        """The job's snapshots, newest first."""
        runtime = self.engine.runtimes[job]
        cached = self._snapshots.get(job)
        marker = self._marker()
        if cached is not None and cached[0] is marker and not refresh:
            return cached[1]
        try:
            output = await self._rustic(job, commands.snapshots_command(runtime), "snapshots")
        except LockTimeout:
            if cached is None:
                raise BrowseError(409, _BUSY) from None
            return cached[1] | {"stale": True}
        try:
            groups = json.loads(output)
            found = [
                _snapshot(raw)
                for group in groups
                for raw in group.get("snapshots", [])
                if _in_scope(raw, runtime)
            ]
            found.sort(key=lambda item: parse_time(item["time"]), reverse=True)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("job %s: unreadable output from `rustic snapshots`: %s", job, exc)
            raise BrowseError(502, "unreadable output from `rustic snapshots`") from None
        payload = {
            "job": job,
            "fetched_at": self.engine.clock().isoformat(),
            "stale": False,
            "snapshots": found,
        }
        self._snapshots[job] = (marker, payload)
        return payload

    async def files(self, job: str, snapshot: str, path: str) -> dict[str, Any]:
        """One directory of one of the job's snapshots; directories first, then by name."""
        if not _SNAPSHOT_ID.fullmatch(snapshot):
            raise BrowseError(400, "the snapshot must be given by its full id")
        path = normalise(path)
        key = (job, snapshot, path)
        if key in self._directories:
            self._directories.move_to_end(key)
            return self._directories[key]
        try:
            # A job may share its repository with others: only its own snapshots are shown.
            known = await self.snapshots(job)
            if not any(item["id"] == snapshot for item in known["snapshots"]):
                raise BrowseError(404, "not one of this job's snapshots; refresh the list")
            runtime = self.engine.runtimes[job]
            output = await self._rustic(
                job, commands.tree_command(runtime, snapshot, path), "files"
            )
        except LockTimeout:
            raise BrowseError(409, _BUSY) from None
        try:
            entries = [_entry(node) for node in json.loads(output)["nodes"]]
            entries.sort(key=lambda item: (item["type"] != "dir", item["name"]))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("job %s: unreadable output from `rustic cat tree`: %s", job, exc)
            raise BrowseError(502, "unreadable output from `rustic cat tree`") from None
        limit = self.config.max_entries
        payload = {
            "job": job,
            "snapshot": snapshot,
            "path": path,
            "total": len(entries),
            "truncated": len(entries) > limit,
            "entries": entries[:limit],
        }
        self._directories[key] = payload
        while len(self._directories) > _CACHED_DIRECTORIES:
            self._directories.popitem(last=False)
        return payload
