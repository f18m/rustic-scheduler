"""Parsing of human-friendly durations such as ``90s``, ``15m`` or ``1h30m``."""

from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TOKEN = re.compile(r"(\d+)([smhdw])")


def parse_duration(value: object) -> timedelta:
    """Turn ``"1h30m"`` or a number of seconds into a ``timedelta``.

    Raises ``ValueError`` for anything else, including zero-length strings and
    negative values, so a typo never silently becomes "no timeout".
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError("a duration cannot be a boolean")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("a duration cannot be negative")
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        raise ValueError(f"cannot read {value!r} as a duration")
    text = value.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    if text.isdigit():
        return timedelta(seconds=int(text))
    pos = 0
    total = 0
    for match in _TOKEN.finditer(text):
        if match.start() != pos:
            break
        total += int(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text):
        raise ValueError(
            f"cannot read {value!r} as a duration; use forms like 30s, 15m, 12h, 7d or 1h30m"
        )
    return timedelta(seconds=total)


def format_duration(value: timedelta | float) -> str:
    """Compact rendering used in logs and notifications (``1h 02m 03s``)."""
    seconds = int(value.total_seconds() if isinstance(value, timedelta) else value)
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
