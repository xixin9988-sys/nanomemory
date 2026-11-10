"""Shared pytest fixtures.

Every fixture writes into a per-test directory under ``<project>/.pytest-tmp`` so tests
never touch the repository's real ``storage/`` directory (spec §15: tests must not operate on
real user data).

Why not pytest's built-in ``tmp_path``?  On this Windows host, ``mkdir(mode=0o700)`` (which
pytest's temp factory uses) produces a directory the owner cannot subsequently list, so the
built-in fixture is unusable here.  The override below creates directories with default
permissions and removes them afterwards.  This is an environment workaround, not a product
design choice.
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from nanomemory.core.config import Settings, build_settings
from nanomemory.core.locks import LockManager

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TMP_ROOT = _PROJECT_ROOT / ".tmpx"
_counter = itertools.count(1)

# ---------------------------------------------------------------------------
# Environment workaround (Windows + this host's file policy)
#
# ``tempfile.mkdtemp`` creates directories with mode 0o700, which on this machine produces a
# directory the owner cannot reopen, and the system temp directory lives outside the writable
# workspace.  Tests therefore point tempfile at a workspace directory and create those
# directories with default permissions.  This is a host limitation, not a product decision.
# ---------------------------------------------------------------------------
_SYS_TMP = _TMP_ROOT / "_sys"
_SYS_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_SYS_TMP)
_original_mkdtemp = tempfile.mkdtemp


def _safe_mkdtemp(
    suffix: str | None = None, prefix: str | None = None, dir: str | None = None
) -> str:
    base = dir or tempfile.gettempdir()
    name = os.path.join(base, (prefix or "tmp") + uuid.uuid4().hex[:10] + (suffix or ""))
    os.makedirs(name, exist_ok=False)
    return name


tempfile.mkdtemp = _safe_mkdtemp  # type: ignore[assignment]


@pytest.fixture()
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Per-test temporary directory created with safe (default) permissions.

    The name is deliberately short: Windows' 260-character path limit is easy to exceed once
    storage/, working/<uuid>/, .txn/<32 hex>/ and a temp filename are appended.
    """
    del request  # the directory name only needs to be unique, not descriptive
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"t{next(_counter):04d}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ambient NANOMEMORY_* variables from leaking into tests."""
    for key in list(os.environ):
        if key.startswith("NANOMEMORY_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Hermetic settings pointing at a temporary storage root."""
    return build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        log_level="WARNING",
        metrics_enabled=True,
    )


@pytest.fixture()
def storage_root(settings: Settings) -> Path:
    root = settings.storage_root
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def lock_manager(settings: Settings, storage_root: Path) -> LockManager:
    return LockManager(
        settings.locks_dir,
        storage_root=storage_root,
        default_timeout=settings.lock_timeout_seconds,
    )


@pytest.fixture()
def session_id() -> str:
    """A deterministic, UUID4-shaped session id."""
    return "123e4567-e89b-42d3-a456-426614174000"


@pytest.fixture()
def other_session_id() -> str:
    return "223e4567-e89b-42d3-a456-426614174001"


@pytest.fixture()
def fresh_uuid() -> str:
    return str(uuid.uuid4())


class RecordingAudit:
    """Test double for the episodic audit sink."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def __call__(self, **event: object) -> None:
        self.events.append(dict(event))

    def types(self) -> list[str]:
        return [str(event.get("event_type")) for event in self.events]

    def of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event.get("event_type") == event_type]


@pytest.fixture()
def recording_audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture()
def caplog_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    yield caplog


def _create_link(target: Path, link: Path, *, directory: bool) -> str:
    """Create a symlink, or a Windows junction when symlink privilege is unavailable.

    Junctions are a reparse point and are equally dangerous for path containment, so they
    are the realistic stand-in on Windows CI machines without ``SeCreateSymbolicLink``.
    """
    try:
        os.symlink(target, link, target_is_directory=directory)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and directory:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and link.exists():
            return "junction"
    raise RuntimeError("this platform does not permit creating symlinks or junctions")


@pytest.fixture()
def make_link() -> Callable[..., str]:
    """Return a helper that links ``target`` at ``link`` or skips the test."""

    def factory(target: Path, link: Path, *, directory: bool = False) -> str:
        try:
            return _create_link(target, link, directory=directory)
        except RuntimeError:
            pytest.skip("symlink/junction creation not permitted on this platform")

    return factory
