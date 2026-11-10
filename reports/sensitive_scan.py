"""Scan the repository for accidental credential material.

Fails when it finds something that looks like a real credential outside an explicit
fixture/placeholder.  Test fixtures use obviously fake, repeated patterns; the scanner only
alerts on files where a secret-like value is not part of a documented fake fixture.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".tmpx",
    ".pytest-tmp",
    ".pt-run1",
    ".probe-mode",
    ".piptmp",
    ".uvcache",
    ".ruff_cache",
    "storage",
    "reports",
    "__pycache__",
    "node_modules",
}
EXCLUDED_SUFFIXES = {".pyc", ".png", ".jpg", ".zip", ".whl", ".xml", ".log"}

PATTERNS = {
    "openai_like": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "anthropic_like": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "github_like": re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws_like": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer_literal": re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{16,}"),
}

#: Files that intentionally contain fake credentials for tests or documentation.
ALLOWED_FILES = {
    "tests/unit/test_secret_and_validation.py",
    "tests/unit/test_logging_redaction.py",
    "nanomemory/core/redaction.py",
    "nanomemory/core/secret_scanner.py",
    "docs/SECURITY.md",
    "README.md",
}


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    hits: list[tuple[str, str, int]] = []
    for path in iter_files():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append((rel, name, line_no))
    if hits:
        print("potential credentials found:")
        for rel, name, line_no in hits:
            print(f"  {rel}:{line_no} ({name})")
        return 1
    scanned = len(iter_files())
    print(f"no credential-like material found outside allowed fixture files ({scanned} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
