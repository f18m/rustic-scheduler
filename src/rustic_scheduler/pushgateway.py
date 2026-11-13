"""Checks that Rustic's own metrics really reached the Pushgateway.

Rustic 0.11.4 pushes its `rustic_backup_*` gauges with one protobuf POST per
snapshot and only logs a warning when that fails; the backup still exits 0. So
delivery is verified here by asking the Pushgateway, never inferred.

Grouping key used by Rustic: `job` (default `rustic_backup`) plus `paths`,
`hostname`, `snapshot_label` and `tags` (empty values are left out).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

#: A metric every Rustic push contains.
MARKER_METRIC = "rustic_backup_time"


@dataclass
class Group:
    labels: dict[str, str]
    push_time: float | None
    last_push_successful: bool
    has_rustic_metrics: bool

    def delete_path(self) -> str:
        def b64(value: str) -> str:
            return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")

        parts = ["metrics", "job@base64", b64(self.labels.get("job", ""))]
        for name, value in sorted(self.labels.items()):
            if name != "job" and value:
                parts += [f"{name}@base64", b64(value)]
        return "/".join(parts)


@dataclass
class Verification:
    result: str  # delivered | missing | stale | unreachable
    push_time: float | None = None
    detail: str = ""
    stale_groups_deleted: int = 0


def _first_value(family: Any) -> float | None:
    try:
        return float(family["metrics"][0]["value"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def parse_groups(payload: Any) -> list[Group]:
    groups = []
    for item in (payload or {}).get("data") or []:
        labels = {str(k): str(v) for k, v in (item.get("labels") or {}).items()}
        groups.append(
            Group(
                labels=labels,
                push_time=_first_value(item.get("push_time_seconds")),
                last_push_successful=bool(item.get("last_push_successful", True)),
                has_rustic_metrics=MARKER_METRIC in item,
            )
        )
    return groups


def groups_of_job(groups: list[Group], label: str, host: str | None) -> list[Group]:
    return [
        group
        for group in groups
        if group.labels.get("snapshot_label") == label
        and (host is None or group.labels.get("hostname") == host)
    ]


class PushgatewayClient:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def groups(self) -> list[Group]:
        async with (
            aiohttp.ClientSession(timeout=self.timeout) as session,
            session.get(f"{self.url}/api/v1/metrics") as response,
        ):
            response.raise_for_status()
            return parse_groups(await response.json())

    async def delete(self, group: Group) -> None:
        async with (
            aiohttp.ClientSession(timeout=self.timeout) as session,
            session.delete(f"{self.url}/{group.delete_path()}") as response,
        ):
            response.raise_for_status()

    async def verify(
        self, label: str, host: str | None, since: datetime, *, cleanup_stale: bool = False
    ) -> Verification:
        """Was a group of this job pushed successfully at or after ``since``?"""
        try:
            mine = groups_of_job(await self.groups(), label, host)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return Verification("unreachable", detail=f"{type(exc).__name__}: {exc}")
        fresh = [
            group
            for group in mine
            if group.has_rustic_metrics
            and group.last_push_successful
            and group.push_time is not None
            # one second of slack for timestamp rounding between the two clocks
            and group.push_time >= since.timestamp() - 1
        ]
        if not fresh:
            newest = max((g.push_time for g in mine if g.push_time is not None), default=None)
            if newest is None:
                return Verification("missing", detail="no metrics group for this job")
            return Verification("stale", newest, "the newest push predates this backup")
        verification = Verification(
            "delivered", max(g.push_time for g in fresh if g.push_time is not None)
        )
        if cleanup_stale:
            for group in mine:
                if group in fresh:
                    continue
                try:
                    await self.delete(group)
                    verification.stale_groups_deleted += 1
                except (aiohttp.ClientError, TimeoutError) as exc:
                    log.warning("could not delete a stale Pushgateway group: %s", exc)
        return verification
