"""Cross-process resource locking (spec §22.2, §24.2, §28.2, §34.4).

Design
------
* One :class:`~filelock.FileLock` per resource file (OS-backed, cross-process).
* One process-wide :class:`threading.Lock` per resource on top of it, so concurrent API
  threads exclude each other as well (filelock alone is process-scoped).
* Fixed global acquisition order; requesting resources in a different order is impossible
  because the manager always sorts by the canonical matrix.
* Every acquisition has a timeout (default 5 s, ``NANOMEMORY_LOCK_TIMEOUT_SECONDS``) and
  raises :class:`~nanomemory.core.errors.LockTimeoutError` instead of waiting forever.
* Reentrant for the same thread/resource so a transaction can call helpers that take the
  same lock without deadlocking.

The canonical order is ``maintenance -> backup -> working -> long_term -> episodic ->
metrics``; release always happens in reverse.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from nanomemory.core.errors import LockTimeoutError
from nanomemory.core.paths import ensure_dir, safe_component

#: Canonical lock order.  Do not reorder; the migration/restore paths depend on it.
LOCK_ORDER: tuple[str, ...] = (
    "maintenance",
    "backup",
    "working",
    "long_term",
    "episodic",
    "metrics",
)

LOCK_ORDER_INDEX: dict[str, int] = {name: index for index, name in enumerate(LOCK_ORDER)}

#: Permission bits for lock files.  They hold no data; 0o644 avoids a Windows quirk where
#: creating a file with owner-only permissions yields a DACL the owner cannot reopen.
LOCK_FILE_MODE = 0o644


@dataclass(slots=True)
class LockEvent:
    """Instrumentation record used by tests and diagnostics (never contains secrets)."""

    action: str
    resource: str
    pid: int
    thread: str
    at: float
    timeout: float | None = None
    operation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resource_class(resource: str) -> str:
    """Return the lock class of ``resource`` (``working:<session>`` -> ``working``)."""
    return resource.split(":", 1)[0]


class LockManager:
    """Ordered, timed, instrumented resource lock manager."""

    def __init__(
        self,
        locks_dir: Path,
        *,
        storage_root: Path | None = None,
        default_timeout: float = 5.0,
        on_event: Callable[[LockEvent], None] | None = None,
    ) -> None:
        self.locks_dir = Path(locks_dir)
        self.storage_root = (
            Path(storage_root) if storage_root is not None else self.locks_dir.parent
        )
        self.default_timeout = float(default_timeout)
        self._on_event = on_event
        self._file_locks: dict[str, FileLock] = {}
        self._thread_locks: dict[str, threading.Lock] = {}
        self._depths: dict[tuple[str, int], int] = defaultdict(int)
        self.events: list[LockEvent] = []
        self._events_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _record(
        self, action: str, resource: str, timeout: float | None, operation: str
    ) -> LockEvent:
        event = LockEvent(
            action=action,
            resource=resource,
            pid=os.getpid(),
            thread=threading.current_thread().name,
            at=time.time(),
            timeout=timeout,
            operation=operation,
        )
        with self._events_lock:
            self.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # pragma: no cover - audit must never break locking
                pass
        return event

    def _lock_file(self, resource: str) -> Path:
        cls = resource_class(resource)
        suffix = resource.split(":", 1)[1] if ":" in resource else ""
        filename = cls if not suffix else f"{cls}-{safe_component(suffix, label='lock suffix')}"
        return self.locks_dir / f"{filename}.lock"

    def _get_file_lock(self, resource: str) -> FileLock:
        lock = self._file_locks.get(resource)
        if lock is None:
            lock = FileLock(
                str(self._lock_file(resource)), timeout=self.default_timeout, mode=LOCK_FILE_MODE
            )
            self._file_locks[resource] = lock
        return lock

    def _get_thread_lock(self, resource: str) -> threading.Lock:
        lock = self._thread_locks.get(resource)
        if lock is None:
            lock = threading.Lock()
            self._thread_locks[resource] = lock
        return lock

    @staticmethod
    def _normalise(resources: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for resource in resources:
            cls = resource_class(resource)
            if cls not in LOCK_ORDER_INDEX:
                raise ValueError(f"unknown lock resource class: {cls!r}")
            if resource not in seen:
                seen.add(resource)
                unique.append(resource)
        return sorted(unique, key=lambda item: (LOCK_ORDER_INDEX[resource_class(item)], item))

    # ------------------------------------------------------------------ acquire
    @contextmanager
    def acquire(
        self,
        resources: Sequence[str],
        *,
        timeout: float | None = None,
        operation: str = "",
    ) -> Iterator[tuple[str, ...]]:
        """Acquire ``resources`` in canonical order; release in reverse order."""
        ordered = self._normalise(resources)
        if not ordered:
            yield ()
            return
        effective_timeout = self.default_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + effective_timeout
        ensure_dir(self.locks_dir, root=self.storage_root, label="locks directory")

        acquired: list[tuple[str, bool]] = []  # (resource, newly_acquired)
        try:
            for resource in ordered:
                key = (resource, threading.get_ident())
                if self._depths[key] > 0:  # reentrant within the same thread
                    self._depths[key] += 1
                    acquired.append((resource, False))
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                self._record("acquire_attempt", resource, effective_timeout, operation)
                thread_lock = self._get_thread_lock(resource)
                if not thread_lock.acquire(timeout=remaining):
                    self._record("acquire_timeout", resource, effective_timeout, operation)
                    raise LockTimeoutError(resource, effective_timeout, {"operation": operation})
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    file_lock = self._get_file_lock(resource)
                    file_lock.timeout = remaining
                    file_lock.acquire()
                except Timeout as exc:
                    thread_lock.release()
                    self._record("acquire_timeout", resource, effective_timeout, operation)
                    raise LockTimeoutError(
                        resource, effective_timeout, {"operation": operation}
                    ) from exc
                except BaseException:
                    thread_lock.release()
                    raise
                self._depths[key] = 1
                self._record("acquire_ok", resource, effective_timeout, operation)
                acquired.append((resource, True))
            yield tuple(ordered)
        finally:
            for resource, newly_acquired in reversed(acquired):
                key = (resource, threading.get_ident())
                if not newly_acquired:
                    if self._depths[key] > 0:
                        self._depths[key] -= 1
                    continue
                self._depths[key] = 0
                try:
                    self._get_file_lock(resource).release()
                finally:
                    self._get_thread_lock(resource).release()
                    self._record("release", resource, effective_timeout, operation)

    # ------------------------------------------------------------------ diagnostics
    def events_since(self, index: int) -> list[LockEvent]:
        with self._events_lock:
            return list(self.events[index:])

    def event_dicts(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return [event.as_dict() for event in self.events]

    def clear_events(self) -> None:
        with self._events_lock:
            self.events.clear()

    def is_held_by_current_thread(self, resource: str) -> bool:
        return self._depths.get((resource, threading.get_ident()), 0) > 0

    def probe(self, resource: str, *, timeout: float = 0.2) -> bool:
        """Try to acquire and immediately release ``resource`` (health checks)."""
        try:
            with self.acquire([resource], timeout=timeout, operation="probe"):
                return True
        except LockTimeoutError:
            return False


def probe_file_lock(path: Path, *, timeout: float = 0.2) -> bool:
    """Probe an arbitrary lock file with a short timeout (used by ``/health``)."""
    lock = FileLock(str(path), timeout=timeout, mode=LOCK_FILE_MODE)
    try:
        lock.acquire()
    except Timeout:
        return False
    except OSError:
        return False
    else:
        lock.release()
        return True


__all__ = [
    "LOCK_ORDER",
    "LOCK_ORDER_INDEX",
    "LockEvent",
    "LockManager",
    "probe_file_lock",
    "resource_class",
]
