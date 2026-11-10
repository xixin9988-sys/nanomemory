"""Durable, atomic filesystem primitives (spec §22.3, §28.4, §33.2, §34.1).

Write protocol used everywhere:

    unique temp file in the *same directory*
        -> write bytes
        -> flush()
        -> os.fsync(file descriptor)
        -> [caller validates content/hash/temp name]
        -> os.replace(temp, target)
        -> fsync(parent directory) when the platform supports it

A unique temp name in the same directory keeps ``os.replace`` atomic on the same
filesystem.  ``fsync`` of a parent directory is not possible on Windows; the helper reports
that honestly instead of pretending durability was achieved (documented in
``docs/SECURITY.md``).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.paths import is_link_like

#: ``.<basename>.nanomemory-tmp.<pid>.<uuid4hex>`` (spec §28.4).
TEMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\.(?P<basename>.+)\.nanomemory-tmp\.(?P<pid>\d+)\.(?P<uuid>[0-9a-f]{32})$"
)

#: ``journal.prepared.tmp.<pid>.<uuid4hex>`` inside ``.txn/<txn_id>/`` (spec §34.1/§36.4).
JOURNAL_TEMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^journal\.prepared\.tmp\.(?P<pid>\d+)\.(?P<uuid>[0-9a-f]{32})$"
)

DIR_FSYNC_SUPPORTED: Final[bool] = os.name != "nt"


@dataclass(slots=True)
class WriteResult:
    """Outcome of an atomic write, including durability facts."""

    path: Path
    sha256: str
    size: int
    directory_fsynced: bool


def temp_name_for(target: Path) -> str:
    """Build the mandated temp basename for ``target``."""
    return f".{target.name}.nanomemory-tmp.{os.getpid()}.{uuid.uuid4().hex}"


def journal_temp_name() -> str:
    """Build the mandated journal temp basename."""
    return f"journal.prepared.tmp.{os.getpid()}.{uuid.uuid4().hex}"


def fsync_dir(directory: Path) -> bool:
    """fsync a directory so a rename is durable; returns False where unsupported.

    POSIX filesystems support this.  Windows does not expose directory fsync through
    Python's ``os`` module, so the call is a documented no-op that returns ``False``.
    """
    if not DIR_FSYNC_SUPPORTED:
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(directory), flags)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def fsync_file(path: Path) -> None:
    """Flush an existing file to disk."""
    with open(path, "r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Streaming SHA-256 of a file's bytes."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def replace_with_retry(source: Path | str, destination: Path | str, *, attempts: int = 5) -> None:
    """``os.replace`` with a short bounded retry for Windows sharing violations.

    Antivirus scanners, OneDrive sync and indexers can briefly hold a freshly written file,
    which makes ``os.replace`` fail with access-denied.  The rename is atomic, so retrying a
    transient denial is safe; a persistent failure is still raised to the caller.
    """
    last: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    if last is not None:
        raise last


def write_bytes_durable(path: Path, data: bytes) -> WriteResult:
    """Write ``data`` to ``path`` atomically and durably (without validating content)."""
    path = Path(path)
    directory = path.parent
    temp_path = directory / temp_name_for(path)
    directory_fsynced = False
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp_path, path)
        directory_fsynced = fsync_dir(directory)
    except BaseException:
        # Never leave a half-written temp behind when the write itself failed.
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise
    return WriteResult(
        path=path, sha256=sha256_bytes(data), size=len(data), directory_fsynced=directory_fsynced
    )


def atomic_write_bytes(path: Path, data: bytes) -> WriteResult:
    """Atomic write of raw bytes, mapping OS errors to NanoMemory error codes."""
    try:
        return write_bytes_durable(path, data)
    except PermissionError as exc:
        raise NanoMemoryError(
            code=ErrorCode.PERMISSION_DENIED,
            message="Write failed: permission denied.",
            details={"target": Path(path).name},
        ) from exc
    except OSError as exc:
        code = ErrorCode.DISK_FULL if _is_disk_full(exc) else ErrorCode.IO_ERROR
        raise NanoMemoryError(
            code=code,
            message="Write failed: filesystem error.",
            details={"target": Path(path).name, "errno": exc.errno},
        ) from exc


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> WriteResult:
    """Atomic write of text."""
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> WriteResult:
    """Atomic write of a JSON document (UTF-8, trailing newline, deterministic order)."""
    data = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False) + "\n"
    return atomic_write_text(path, data)


def read_text(path: Path, *, max_bytes: int | None = None) -> str:
    """Read UTF-8 text; optionally enforce a maximum byte size."""
    raw = read_bytes(path, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NanoMemoryError(
            code=ErrorCode.CORRUPT_DATA,
            message="File is not valid UTF-8.",
            details={"target": Path(path).name},
        ) from exc


def read_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read bytes, raising a typed error for missing or oversized files."""
    path = Path(path)
    try:
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise NanoMemoryError(
                code=ErrorCode.FILE_TOO_LARGE,
                message="File exceeds the configured maximum size.",
                details={"target": path.name, "max_bytes": max_bytes},
            )
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise NanoMemoryError(
            code=ErrorCode.NOT_FOUND,
            message="File not found.",
            details={"target": path.name},
        ) from exc
    except PermissionError as exc:
        raise NanoMemoryError(
            code=ErrorCode.PERMISSION_DENIED,
            message="Read failed: permission denied.",
            details={"target": path.name},
        ) from exc


def read_json(path: Path, *, max_bytes: int | None = None) -> Any:
    """Read and parse JSON, raising :class:`NanoMemoryError` with ``corrupt_data`` on failure."""
    text = read_text(path, max_bytes=max_bytes)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise NanoMemoryError(
            code=ErrorCode.CORRUPT_DATA,
            message="File contains invalid JSON.",
            details={"target": Path(path).name, "line": exc.lineno, "column": exc.colno},
        ) from exc


def is_temp_name(name: str) -> bool:
    """True when ``name`` matches the mandated general atomic-write temp pattern."""
    return bool(TEMP_PATTERN.match(name))


def is_journal_temp_name(name: str) -> bool:
    """True when ``name`` matches the mandated prepared-journal temp pattern."""
    return bool(JOURNAL_TEMP_PATTERN.match(name))


def _is_disk_full(exc: OSError) -> bool:
    import errno

    return exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


def find_orphan_temps(
    directory: Path,
    *,
    max_age_seconds: int,
    is_referenced: Callable[[Path], bool] | None = None,
    now: float | None = None,
) -> list[Path]:
    """Return general atomic-write temp files that satisfy every §28.4 condition.

    Conditions: correct name pattern, inside ``directory`` (non-symlink), older than
    ``max_age_seconds`` and not referenced by any live journal.  Symlinks and files whose
    metadata cannot be read are never returned.
    """
    now = time.time() if now is None else now
    orphans: list[Path] = []
    if not directory.is_dir():
        return orphans
    for entry in directory.iterdir():
        try:
            if is_link_like(entry) or not entry.is_file():
                continue
            if not is_temp_name(entry.name):
                continue
            stat = entry.stat()
        except OSError:
            continue
        if now - stat.st_mtime <= max_age_seconds:
            continue
        if is_referenced is not None and is_referenced(entry):
            continue
        orphans.append(entry)
    return sorted(orphans)


def remove_file(path: Path) -> None:
    """Delete a file, mapping errors to typed failures (never used on user files blindly)."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except PermissionError as exc:
        raise NanoMemoryError(
            code=ErrorCode.PERMISSION_DENIED,
            message="Delete failed: permission denied.",
            details={"target": Path(path).name},
        ) from exc


def stat_signature(path: Path) -> dict[str, Any]:
    """Lightweight metadata snapshot used by audit events and orphan cleanup."""
    stat = Path(path).stat()
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256_file(path) if stat.st_size <= 8 * 1024 * 1024 else None,
    }


__all__ = [
    "DIR_FSYNC_SUPPORTED",
    "JOURNAL_TEMP_PATTERN",
    "TEMP_PATTERN",
    "WriteResult",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "find_orphan_temps",
    "fsync_dir",
    "fsync_file",
    "is_journal_temp_name",
    "is_temp_name",
    "journal_temp_name",
    "read_bytes",
    "read_json",
    "read_text",
    "remove_file",
    "sha256_bytes",
    "sha256_file",
    "stat_signature",
    "temp_name_for",
    "write_bytes_durable",
]
