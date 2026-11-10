"""Weekly wall-clock schedules in a named timezone.

A schedule is a set of weekdays and local times. Every (date, time) pair is
resolved to exactly one UTC instant, which makes daylight-saving behaviour
deterministic:

* A local time that does not exist (spring forward) fires once, shifted forward
  by the length of the gap (02:30 becomes 03:30 when 02:00-03:00 is skipped).
* A local time that occurs twice (fall back) fires once, at its first occurrence.
* Two configured times that resolve to the same instant fire once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ALIASES = {
    **{name: index for index, name in enumerate(DAY_NAMES)},
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_day(value: str) -> int:
    key = value.strip().lower()
    if key not in _ALIASES:
        raise ValueError(f"unknown day {value!r}; use mon, tue, wed, thu, fri, sat, sun or daily")
    return _ALIASES[key]


def parse_time(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        raise ValueError(f"cannot read {value!r} as a time; use 24-hour HH:MM such as 02:30")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"{value!r} is not a valid time of day")
    return time(hour, minute)


def resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """UTC instant for a local wall time, following the module's DST rules."""
    # fold=0 picks the first occurrence of an ambiguous time and, for a
    # nonexistent time, the offset in force before the gap (PEP 495), which
    # lands the instant after the gap by the gap's length.
    return datetime.combine(day, at).replace(tzinfo=tz, fold=0).astimezone(UTC)


@dataclass(frozen=True)
class Schedule:
    days: frozenset[int]
    times: tuple[time, ...]
    tz: ZoneInfo

    def _instants(self, first: date, last: date) -> list[datetime]:
        found: set[datetime] = set()
        day = first
        while day <= last:
            if day.weekday() in self.days:
                found.update(resolve_local(day, at, self.tz) for at in self.times)
            day += timedelta(days=1)
        return sorted(found)

    def next_after(self, after: datetime) -> datetime:
        """First occurrence strictly later than ``after``."""
        local_day = after.astimezone(self.tz).date()
        for instant in self._instants(local_day - timedelta(days=1), local_day + timedelta(days=8)):
            if instant > after:
                return instant
        raise RuntimeError("schedule has no upcoming occurrence")  # pragma: no cover

    def previous(self, at_or_before: datetime) -> datetime:
        """Latest occurrence at or before the given instant."""
        local_day = at_or_before.astimezone(self.tz).date()
        instants = self._instants(local_day - timedelta(days=8), local_day + timedelta(days=1))
        for instant in reversed(instants):
            if instant <= at_or_before:
                return instant
        raise RuntimeError("schedule has no past occurrence")  # pragma: no cover

    def between(self, after: datetime, until: datetime, limit: int = 1000) -> list[datetime]:
        """Occurrences in ``(after, until]``, oldest first, capped at ``limit``."""
        found: list[datetime] = []
        cursor = after
        while len(found) < limit:
            cursor = self.next_after(cursor)
            if cursor > until:
                break
            found.append(cursor)
        return found

    def describe(self) -> str:
        days = "daily" if len(self.days) == 7 else ",".join(DAY_NAMES[d] for d in sorted(self.days))
        times = ",".join(t.strftime("%H:%M") for t in self.times)
        return f"{days} at {times} ({self.tz.key})"
