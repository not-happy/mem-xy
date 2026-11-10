"""Secret scrubbing for anything that reaches the DB or the UI.

`jobs.last_error` and `recall_log` are shown in the debug UI, and httpx exception
strings routinely embed request headers — so a leaked token there is a bug, not a
policy question (plan §2.1).
"""

from __future__ import annotations

import re
from typing import Iterable

REDACTED = "***REDACTED***"

_PATTERNS = [
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(x[-_]api[-_]key\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(token\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(password\s*[:=]\s*)\S+"),
    re.compile(r"(?i)\b(passwd\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(\bpassword=)[^&\s'\"]+"),
    re.compile(r"(?i)(://[^/\s:@]+:)[^@\s]+(@)"),
]

_known_secrets: list[str] = []


def register_secrets(values: Iterable[str | None]) -> None:
    """Register literal secret values so they are scrubbed wherever they appear."""
    for value in values:
        if value and len(value) >= 6 and value not in _known_secrets:
            _known_secrets.append(value)


def redact(text: str | None) -> str | None:
    if not text:
        return text
    out = str(text)
    for secret in _known_secrets:
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        groups = pattern.groups
        if groups == 2:
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", out)
        elif groups == 1:
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def redact_mapping(mapping: dict | None) -> dict | None:
    if mapping is None:
        return None
    return {k: redact(v) if isinstance(v, str) else v for k, v in mapping.items()}
