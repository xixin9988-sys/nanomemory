"""Secret redaction for logs and error surfaces (spec §29.8).

Redaction is applied to the *final formatted text* of every log record, which covers the
message, structured extra fields, exception text and third-party loggers routed through the
root handlers.  ``request_id`` is deliberately preserved: it is operational metadata, not a
credential.

Patterns mirror the secret scanner's explicit credential modes.  The scanner additionally
looks at entropy; redaction only needs the deterministic patterns because log lines rarely
carry a bare high-entropy blob without a credential prefix.
"""

from __future__ import annotations

import re
from typing import Any, Final

REDACTED: Final[str] = "***REDACTED***"

#: ``(name, pattern, replacement)`` applied in order.  The Authorization header keeps its
#: scheme word so the log stays debuggable.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    (
        "authorization_header",
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)\s*:\s*(?:bearer|basic|token)?\s*[^\s,;\"']+"
        ),
        r"\1: " + REDACTED,
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
        "Bearer " + REDACTED,
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), REDACTED),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), REDACTED),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{12,}"), REDACTED),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
)


def redact(text: str) -> str:
    """Return ``text`` with credential material replaced by :data:`REDACTED`.

    The function is idempotent: redacting an already redacted string is a no-op.
    """
    if not text:
        return text
    result = text
    for _name, pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values inside a mapping/list structure."""
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, str):
            cleaned[key] = redact(value)
        elif isinstance(value, dict):
            cleaned[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            items: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    items.append(redact(item))
                elif isinstance(item, dict):
                    items.append(redact_mapping(item))
                else:
                    items.append(item)
            cleaned[key] = items
        else:
            cleaned[key] = value
    return cleaned


def contains_redaction_marker(text: str) -> bool:
    """Test helper: was anything redacted in ``text``?"""
    return REDACTED in text


__all__ = ["REDACTED", "contains_redaction_marker", "redact", "redact_mapping"]
