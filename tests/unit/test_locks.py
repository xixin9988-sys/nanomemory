"""Unit tests for the resource lock manager (spec §24.2, §18 §34.4)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nanomemory.core.errors import LockTimeoutError
from nanomemory.core.locks import LOCK_ORDER, LockManager, probe_file_lock, resource_class


def test_lock_files_created_under_locks_dir(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "storage" / ".locks", storage_root=tmp_path / "storage")
    with manager.acquire(["working:sess-1"]):
        # filelock removes the lock file on release on this platform, so assert while held.
        assert (tmp_path / "storage" / ".locks" / "working-sess-1.lock").exists()
    assert (tmp_path / "storage" / ".locks").is_dir()


def test_acquisition_follows_canonical_order(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    requested = ["metrics", "working", "maintenance", "episodic"]
    with manager.acquire(requested):
        pass
    acquired = [e.resource for e in manager.events if e.action == "acquire_ok"]
    assert acquired == ["maintenance", "working", "episodic", "metrics"]


def test_release_order_is_reverse(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    with manager.acquire(["metrics", "working", "backup"]):
        pass
    released = [e.resource for e in manager.events if e.action == "release"]
    assert released == ["metrics", "working", "backup"]


def test_unknown_resource_class_rejected(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    with pytest.raises(ValueError):
        with manager.acquire(["unknown_lock"]):
            pass


def test_reentrant_acquire_same_thread(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    with manager.acquire(["working:s1"]):
        with manager.acquire(["working:s1"]):
            assert manager.is_held_by_current_thread("working:s1")
        assert manager.is_held_by_current_thread("working:s1")
    assert not manager.is_held_by_current_thread("working:s1")


def test_lock_timeout_in_other_thread(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path, default_timeout=0.2)
    errors: list[Exception] = []

    def contender() -> None:
        try:
            with manager.acquire(["working:s1"], timeout=0.2):
                pass
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    with manager.acquire(["working:s1"]):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
    assert errors and isinstance(errors[0], LockTimeoutError)
    assert errors[0].status == 503
    assert errors[0].details["resource"] == "working:s1"


def test_lock_released_after_exception(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    with pytest.raises(RuntimeError):
        with manager.acquire(["working:s1"]):
            raise RuntimeError("boom")
    with manager.acquire(["working:s1"], timeout=0.5):
        pass


def test_probe_reports_contention(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path)
    with manager.acquire(["working:s1"]):
        assert manager.probe("working:s1", timeout=0.1) is True  # same-thread reentrancy
    assert manager.probe("working:s1", timeout=0.1) is True


def test_probe_file_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "health.lock"
    assert probe_file_lock(lock_path, timeout=0.2) is True


def test_resource_class_parsing() -> None:
    assert resource_class("working:abc") == "working"
    assert resource_class("metrics") == "metrics"


def test_default_timeout_is_bounded(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks", storage_root=tmp_path, default_timeout=0.15)
    started = time.monotonic()

    def holder() -> None:
        with manager.acquire(["episodic"]):
            time.sleep(0.4)

    thread = threading.Thread(target=holder)
    thread.start()
    time.sleep(0.05)
    with pytest.raises(LockTimeoutError):
        with manager.acquire(["episodic"]):
            pass
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    thread.join(timeout=5)


def test_lock_order_constant_is_stable() -> None:
    assert LOCK_ORDER == (
        "maintenance",
        "backup",
        "working",
        "long_term",
        "episodic",
        "metrics",
    )
