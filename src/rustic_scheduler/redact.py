"""Keeps secrets out of logs, run logs, MQTT payloads and notifications."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

MASK = "[REDACTED]"

_PATTERNS = [
    # scheme://user:password@host. Anchored on "://" so that matching stays
    # linear even on very long lines.
    (re.compile(r"(?P<pre>://[^\s/:@]*:)[^\s/@]+(?P<post>@)"), r"\g<pre>" + MASK + r"\g<post>"),
    # key=value / key: value pairs whose key looks secret
    (
        re.compile(
            r"(?i)(?P<pre>(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]{0,40}"
            r"\s{0,4}[=:]\s{0,4})(?P<q>[\"']?)[^\s\"',;]+(?P=q)"
        ),
        r"\g<pre>" + MASK,
    ),
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
]


class Redactor:
    """Masks registered literal secrets and common secret-looking patterns."""

    def __init__(self) -> None:
        self._literals: set[str] = set()

    def register(self, secrets: Iterable[str | None]) -> None:
        for secret in secrets:
            if secret and len(secret.strip()) >= 4:
                self._literals.add(secret.strip())

    def __call__(self, text: str) -> str:
        for literal in sorted(self._literals, key=len, reverse=True):
            text = text.replace(literal, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


#: Process-wide instance; modules register the secrets they load.
REDACTOR = Redactor()


class RedactingFilter(logging.Filter):
    """Logging filter that applies :data:`REDACTOR` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        record.msg = REDACTOR(message)
        record.args = None
        return True
