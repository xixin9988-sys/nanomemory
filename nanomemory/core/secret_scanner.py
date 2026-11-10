"""Secret scanning for memory content (spec §22.7, §29.7, §35.4).

Two layers:

1. **Explicit credential patterns** - OpenAI/Anthropic style ``sk-`` keys, GitHub tokens,
   AWS access keys and PEM private-key blocks.  These are always reported; no allowlist and
   no configuration can suppress them.
2. **Generic high-entropy candidates** - long base64-ish runs with Shannon entropy above
   4.5 bits/char.  Candidates are filtered through the built-in allowlist (MD5/SHA hashes,
   JWT, image data URIs, ordinary file paths/URLs) and an optional extra regex from
   ``NANOMEMORY_SECRET_ALLOWLIST_REGEX``.

A hit never carries the secret itself: only the pattern name, location and offsets, so hit
details are safe to log, return in API errors and persist in audit events.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: ``sk-`` (OpenAI), ``sk-ant-`` (Anthropic), GitHub, AWS, PEM blocks.
EXPLICIT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
)

#: Generic candidate set from the specification.
GENERIC_PATTERN = re.compile(r"[A-Za-z0-9+/=]{40,}")

#: Hex digests of these lengths are common hashes, not secrets.
_HASH_LENGTHS = frozenset({32, 40, 64})
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_JWT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_DATA_URI_PREFIX = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)
_PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|(?:\.{0,2}[\\/])|https?://).*")
#: Long alphabetic words are not secrets; they are prose or identifiers.
_WORD_ONLY = re.compile(r"^[A-Za-z]+$")

ENTROPY_THRESHOLD = 4.5


@dataclass(slots=True)
class SecretHit:
    """A scanner hit.  Deliberately excludes the matched secret text."""

    pattern: str
    location: str
    start: int
    end: int
    entropy: float | None = None
    span_kind: str = "explicit"


@dataclass(slots=True)
class ScanResult:
    """Outcome of scanning one payload."""

    hits: list[SecretHit] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.hits)

    def hit_types(self) -> list[str]:
        seen: list[str] = []
        for hit in self.hits:
            if hit.pattern not in seen:
                seen.append(hit.pattern)
        return seen

    def locations(self) -> list[str]:
        return sorted({hit.location for hit in self.hits})


def shannon_entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _decodes_to_json_object(segment: str) -> bool:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict)


def is_jwt(token: str) -> bool:
    """True when ``token`` looks like a JWT whose header decodes to a JSON object."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    if not all(_JWT_SEGMENT.match(part) for part in parts):
        return False
    return _decodes_to_json_object(parts[0])


def is_jwt_segment(token: str, surrounding: str) -> bool:
    """True when ``token`` is one segment of a JWT embedded in ``surrounding``."""
    for chunk in re.split(r"[\s,;\"'()\[\]{}<>]+", surrounding):
        if token not in chunk:
            continue
        if is_jwt(chunk):
            return True
    return False


def is_image_data_uri_payload(token: str, surrounding: str) -> bool:
    """True when the candidate is the base64 payload of an image data URI."""
    index = surrounding.find(token)
    if index <= 0:
        return False
    prefix = surrounding[:index]
    return bool(_DATA_URI_PREFIX.search(prefix[-80:]))


def _looks_like_path_or_url(token: str) -> bool:
    if _PATH_LIKE.match(token):
        return True
    # "a/b/c/d" style relative fragments without base64 padding characters.
    if token.count("/") >= 2 and "+" not in token and "=" not in token:
        return True
    return False


class SecretScanner:
    """Configurable scanner with the specification's default behaviour."""

    def __init__(
        self,
        *,
        extra_allowlist_regex: str = "",
        entropy_threshold: float = ENTROPY_THRESHOLD,
    ) -> None:
        self.extra_allowlist = (
            re.compile(extra_allowlist_regex) if extra_allowlist_regex.strip() else None
        )
        self.entropy_threshold = entropy_threshold

    # ------------------------------------------------------------------ scanning
    def scan_text(self, text: str, *, location: str = "content") -> ScanResult:
        """Scan a single string and return every hit with its location."""
        result = ScanResult()
        if not text:
            return result

        explicit_spans: list[tuple[int, int]] = []
        for name, pattern in EXPLICIT_PATTERNS:
            for match in pattern.finditer(text):
                result.hits.append(
                    SecretHit(
                        pattern=name,
                        location=location,
                        start=match.start(),
                        end=match.end(),
                        span_kind="explicit",
                    )
                )
                explicit_spans.append((match.start(), match.end()))

        for match in GENERIC_PATTERN.finditer(text):
            token = match.group(0)
            if any(start <= match.start() < end for start, end in explicit_spans):
                continue
            if not self._is_generic_candidate(token, text):
                continue
            entropy = shannon_entropy(token)
            if entropy <= self.entropy_threshold:
                continue
            result.hits.append(
                SecretHit(
                    pattern="high_entropy_string",
                    location=location,
                    start=match.start(),
                    end=match.end(),
                    entropy=round(entropy, 3),
                    span_kind="generic",
                )
            )
        result.hits.sort(key=lambda hit: hit.start)
        return result

    def _is_generic_candidate(self, token: str, surrounding: str) -> bool:
        """Allowlist filtering for generic candidates only (spec §35.4)."""
        if _HEX_ONLY.match(token) and len(token) in _HASH_LENGTHS:
            return False  # common MD5/SHA digest
        if is_jwt(token) or is_jwt_segment(token, surrounding):
            return False
        if is_image_data_uri_payload(token, surrounding):
            return False
        if _looks_like_path_or_url(token):
            return False
        if _WORD_ONLY.match(token):
            return False  # long word, not an encoded secret
        if self.extra_allowlist is not None and self.extra_allowlist.search(token):
            return False
        return True

    def scan_value(self, value: Any, *, location: str = "payload") -> ScanResult:
        """Recursively scan strings inside a JSON-compatible value."""
        result = ScanResult()
        self._scan_into(value, location, result)
        return result

    def _scan_into(self, value: Any, location: str, result: ScanResult) -> None:
        if isinstance(value, str):
            result.hits.extend(self.scan_text(value, location=location).hits)
        elif isinstance(value, dict):
            for key, item in value.items():
                self._scan_into(item, f"{location}.{key}", result)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self._scan_into(item, f"{location}[{index}]", result)
        else:
            return

    # ------------------------------------------------------------------ redaction
    def redact_text(self, text: str) -> tuple[str, ScanResult]:
        """Return ``text`` with hits replaced by a placeholder plus the hit summary.

        Replacement is done from the end so earlier offsets stay valid.  Generic candidates
        are replaced by character count so surrounding prose keeps its shape.
        """
        result = self.scan_text(text)
        if not result.hits:
            return text, result
        redacted = text
        for hit in sorted(result.hits, key=lambda item: item.start, reverse=True):
            redacted = redacted[: hit.start] + "***REDACTED***" + redacted[hit.end :]
        return redacted, result

    def redact_value(self, value: Any) -> tuple[Any, ScanResult]:
        """Return a copy of ``value`` with every string redacted."""
        result = ScanResult()

        def walk(item: Any) -> Any:
            if isinstance(item, str):
                cleaned, scan = self.redact_text(item)
                result.hits.extend(scan.hits)
                return cleaned
            if isinstance(item, dict):
                return {key: walk(val) for key, val in item.items()}
            if isinstance(item, (list, tuple)):
                return [walk(val) for val in item]
            return item

        return walk(value), result


__all__ = [
    "ENTROPY_THRESHOLD",
    "EXPLICIT_PATTERNS",
    "GENERIC_PATTERN",
    "ScanResult",
    "SecretHit",
    "SecretScanner",
    "is_image_data_uri_payload",
    "is_jwt",
    "is_jwt_segment",
    "shannon_entropy",
]
