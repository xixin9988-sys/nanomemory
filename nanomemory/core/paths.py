"""Filesystem path safety helpers (spec §22.8, §24.3, §30.7).

All user- or model-influenced paths must be resolved and proven to stay inside the
configured storage root before any read or write.  Controlled storage directories must not
contain symlinks; a symlink anywhere between the root and the target is rejected rather than
followed.  This closes the classic "check, then replace with a symlink" (TOCTOU) gap as far
as a portable Python implementation reasonably can; the residual platform limitation is
documented in ``docs/SECURITY.md``.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

from nanomemory.core.errors import (
    ErrorCode,
    NanoMemoryError,
    PathNotAllowedError,
    SymlinkDetectedError,
)

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

#: ``FILE_ATTRIBUTE_REPARSE_POINT`` - covers symlinks *and* NTFS junctions on Windows.
_REPARSE_POINT = 0x400


def is_link_like(path: Path) -> bool:
    """True for symlinks and Windows reparse points (junctions, mount points).

    ``Path.is_symlink()`` returns ``False`` for an NTFS junction, yet a junction redirects
    reads and writes exactly like a symlink.  Storage paths must reject both, so the
    attribute-level check is used throughout instead of ``is_symlink()`` alone.
    """
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def normcase(value: str | Path) -> str:
    """Case-normalise a path string (Windows is case-insensitive)."""
    return os.path.normcase(os.fspath(value))


def is_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` (already resolved) lives inside ``root``."""
    root_s, cand_s = normcase(root), normcase(candidate)
    return cand_s == root_s or cand_s.startswith(root_s + os.sep)


def ensure_within(root: Path, candidate: Path, *, label: str = "path") -> Path:
    """Resolve ``candidate`` and assert it stays within ``root``.

    Raises :class:`PathNotAllowedError` on traversal or absolute escape.  The returned path
    is the resolved path, which callers must use for subsequent IO (do not re-use the
    unresolved input).
    """
    root_resolved = Path(root).resolve()
    candidate_resolved = Path(candidate).resolve()
    if not is_within(root_resolved, candidate_resolved):
        raise PathNotAllowedError(
            f"{label} escapes the configured storage root.",
            details={"label": label},
        )
    return candidate_resolved


def find_symlink_component(root: Path, candidate: Path) -> Path | None:
    """Return the first symlinked component between ``root`` and ``candidate``.

    ``candidate`` is treated lexically (it may not exist yet).  ``root`` itself is allowed
    to be a symlink (for example a user-chosen volume mount) but no component below it is.
    """
    root_path = Path(root)
    candidate_path = Path(candidate)
    try:
        relative = candidate_path.relative_to(root_path)
    except ValueError:
        return None
    current = root_path
    for part in relative.parts:
        current = current / part
        if is_link_like(current):
            return current
    return None


def ensure_no_symlink(root: Path, candidate: Path, *, label: str = "path") -> Path:
    """Reject if any component below ``root`` is a link; return resolved path.

    The link check is lexical and happens *before* resolution, so a link that points outside
    the root is reported as :class:`SymlinkDetectedError` rather than a generic traversal
    error.  Both outcomes are rejections; the distinction matters for diagnostics.
    """
    offender = find_symlink_component(Path(root).resolve(), Path(candidate))
    if offender is not None:
        raise SymlinkDetectedError(
            f"{label} contains a symbolic link or junction and is not allowed.",
            details={"label": label},
        )
    return ensure_within(root, candidate, label=label)


def ensure_dir(path: Path, *, root: Path, label: str = "directory") -> Path:
    """Create ``path`` (and parents) under ``root`` while enforcing the symlink policy."""
    offender = find_symlink_component(Path(root).resolve(), Path(path))
    if offender is not None:
        raise SymlinkDetectedError(
            f"{label} contains a symbolic link or junction and is not allowed.",
            details={"label": label},
        )
    ensure_within(root, path, label=label)
    resolved = Path(path).resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise NanoMemoryError(
            code=ErrorCode.IO_ERROR,
            message=f"{label} exists but is not a directory.",
        ) from exc
    except PermissionError as exc:
        raise NanoMemoryError(
            code=ErrorCode.PERMISSION_DENIED,
            message=f"Cannot create {label}: permission denied.",
        ) from exc
    except OSError as exc:
        raise NanoMemoryError(
            code=ErrorCode.IO_ERROR,
            message=f"Cannot create {label}: filesystem error.",
        ) from exc
    return resolved


def safe_component(value: str, *, label: str = "name") -> str:
    """Validate a single path component (session id, backup id, …) against traversal."""
    if not value or not _SAFE_COMPONENT.match(value) or value in {".", ".."}:
        raise PathNotAllowedError(
            f"Invalid {label}: only letters, digits, dot, dash and underscore are allowed.",
            details={"label": label},
        )
    if _DRIVE_PREFIX.match(value):
        raise PathNotAllowedError(f"Invalid {label}: drive letters are not allowed.")
    return value


def join_safe(root: Path, *parts: str, label: str = "path") -> Path:
    """Join validated components onto ``root`` and assert containment."""
    for part in parts:
        safe_component(part, label=label)
    return ensure_within(root, Path(root).joinpath(*parts), label=label)


def relative_posix(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` using forward slashes (manifests, journals)."""
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def human_size(num_bytes: int) -> str:
    """Format a byte count for logs and diagnostics."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def existing_symlinks(root: Path, paths: Iterable[Path]) -> list[Path]:
    """Return the subset of ``paths`` (or their ancestors under root) that are symlinks."""
    found: list[Path] = []
    for candidate in paths:
        offender = find_symlink_component(Path(root).resolve(), Path(candidate))
        if offender is not None:
            found.append(offender)
    return found


__all__ = [
    "ensure_dir",
    "ensure_no_symlink",
    "ensure_within",
    "existing_symlinks",
    "find_symlink_component",
    "human_size",
    "is_link_like",
    "is_within",
    "join_safe",
    "normcase",
    "relative_posix",
    "safe_component",
]
