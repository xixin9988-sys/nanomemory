"""Subprocess worker used by the cross-process tests.

Usage::

    python tests/fixtures/concurrent_worker.py update <storage_root> <session_id> <updates>
    python tests/fixtures/concurrent_worker.py hold-lock <storage_root> <seconds>
    python tests/fixtures/concurrent_worker.py recover <storage_root> <session_id>

The worker prints one JSON object on stdout.  It exists so lock and version guarantees are
verified across **processes**, not just threads (spec §22.24, §33.4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.core.episodic_memory import EpisodicMemoryStore  # noqa: E402
from nanomemory.core.errors import NanoMemoryError  # noqa: E402
from nanomemory.core.locks import LockManager  # noqa: E402
from nanomemory.core.memory_models import GoalStatus, MemoryUpdateProposal  # noqa: E402
from nanomemory.core.memory_validator import MemoryValidator  # noqa: E402
from nanomemory.core.secret_scanner import SecretScanner  # noqa: E402
from nanomemory.core.working_memory import WorkingMemoryStore  # noqa: E402


def _store(storage_root: str) -> WorkingMemoryStore:
    settings = build_settings(
        storage_root=Path(storage_root), lock_timeout_seconds=5.0, environment="test"
    )
    locks = LockManager(
        settings.locks_dir,
        storage_root=settings.storage_root,
        default_timeout=settings.lock_timeout_seconds,
    )
    validator = MemoryValidator(settings, SecretScanner())
    episodic = EpisodicMemoryStore(settings, locks, validator)
    episodic.ensure_storage()

    def audit(**fields: object) -> None:
        episodic.record_audit(
            event_type=str(fields["event_type"]),
            session_id=str(fields.get("session_id", "system")),
            summary=str(fields.get("summary") or fields["event_type"]),
            outcome=str(fields.get("outcome") or "ok"),
        )

    return WorkingMemoryStore(settings, locks, validator, audit=audit)


def _update(storage_root: str, session_id: str, count: int) -> dict[str, object]:
    store = _store(storage_root)
    versions: list[int] = []
    conflicts = 0
    for index in range(count):
        for _attempt in range(50):
            snapshot = store.read(session_id)
            try:
                result = store.update(
                    session_id,
                    MemoryUpdateProposal(
                        current_goal=f"worker update {index}",
                        status=GoalStatus.IN_PROGRESS,
                        in_progress=[f"worker item {index}"],
                        allow_task_removal=True,
                    ),
                    expected_version=snapshot.meta.memory_version,
                )
            except NanoMemoryError as exc:
                if exc.code.value == "version_conflict":
                    conflicts += 1
                    continue
                raise
            versions.append(result.snapshot.meta.memory_version)
            break
        else:
            return {"ok": False, "error": "could not commit", "versions": versions}
    return {"ok": True, "versions": versions, "conflicts": conflicts}


def _hold_lock(storage_root: str, seconds: float) -> dict[str, object]:
    settings = build_settings(storage_root=Path(storage_root))
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root, default_timeout=5.0)
    with locks.acquire(["working"], operation="test_holder"):
        print(json.dumps({"ok": True, "ready": True}), flush=True)
        import time

        time.sleep(seconds)
    return {"ok": True, "released": True}


def _recover(storage_root: str, session_id: str) -> dict[str, object]:
    store = _store(storage_root)
    try:
        outcome = store.recover(session_id)
    except NanoMemoryError as exc:
        return {"ok": False, "code": exc.code.value}
    return {
        "ok": True,
        "failed": outcome.failed,
        "actions": outcome.actions,
        "state": outcome.state.value,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "mode required"}))
        return 2
    mode = argv[1]
    if mode == "update":
        payload = _update(argv[2], argv[3], int(argv[4]))
    elif mode == "hold-lock":
        payload = _hold_lock(argv[2], float(argv[3]))
    elif mode == "recover":
        payload = _recover(argv[2], argv[3])
    else:
        payload = {"ok": False, "error": f"unknown mode {mode}"}
    print(json.dumps(payload), flush=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
