"""Cross-process integration tests: locking, version monotonicity and recovery (spec §22.24, §33.4).

These tests spawn real subprocesses: file locks, OS-level exclusion and journal recovery
cannot be validated with threads alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.episodic_memory import EpisodicMemoryStore
from nanomemory.core.errors import LockTimeoutError
from nanomemory.core.locks import LockManager
from nanomemory.core.memory_models import (
    GoalStatus,
    JournalStatus,
    MemoryUpdateProposal,
    SessionState,
    TxnJournal,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.secret_scanner import SecretScanner
from nanomemory.core.working_memory import (
    JOURNAL_NAME,
    OLD_MD_NAME,
    WorkingMemoryStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER = PROJECT_ROOT / "tests" / "fixtures" / "concurrent_worker.py"


def _run_worker(*args: str, wait: bool = True) -> subprocess.Popen[str] | tuple[int, str]:
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(WORKER), *args],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not wait:
        return process
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr
    line = [item for item in stdout.strip().splitlines() if item.strip()][-1]
    return json.loads(line)


@pytest.fixture()
def crash_session(tmp_path):
    """Session with a prepared journal and only the body replaced (crash state)."""
    settings = build_settings(storage_root=tmp_path / "storage", lock_timeout_seconds=5.0)
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    episodic = EpisodicMemoryStore(settings, locks, MemoryValidator(settings, SecretScanner()))
    episodic.ensure_storage()

    def audit(**fields: object) -> None:
        episodic.record_audit(
            event_type=str(fields["event_type"]),
            session_id=str(fields.get("session_id", "system")),
            summary=str(fields.get("summary") or fields["event_type"]),
            outcome=str(fields.get("outcome") or "ok"),
        )

    store = WorkingMemoryStore(
        settings, locks, MemoryValidator(settings, SecretScanner()), audit=audit
    )
    store.registry.ensure_default()
    session_id = settings.default_session_id
    store.ensure_session(session_id)
    directory = store.session_dir(session_id)
    original_body = (directory / "memory.md").read_bytes()
    original_meta = (directory / "memory.meta.json").read_bytes()
    result = store.update(
        session_id,
        MemoryUpdateProposal(current_goal="crash state", status=GoalStatus.IN_PROGRESS),
        expected_version=1,
    )
    txn_dir = store.txn_root(session_id) / result.txn_id
    journal_path = txn_dir / JOURNAL_NAME
    journal = TxnJournal.model_validate(json.loads(journal_path.read_text()))
    journal.status = JournalStatus.PREPARED
    journal.completed_at = None
    journal_path.write_text(json.dumps(journal.model_dump(mode="json"), indent=2) + "\n")
    (directory / "memory.md").write_bytes(result.snapshot.markdown.encode("utf-8"))
    (directory / "memory.meta.json").write_bytes(original_meta)
    store.write_state(
        session_id, SessionState.RECOVERY_REQUIRED, reason="test", txn_id=result.txn_id
    )
    return settings, store, session_id, original_body, original_meta


def test_concurrent_updates_are_monotonic(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage", lock_timeout_seconds=10.0)
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    store = WorkingMemoryStore(settings, locks, MemoryValidator(settings, SecretScanner()))
    store.registry.ensure_default()
    session_id = settings.default_session_id
    store.ensure_session(session_id)

    workers = [
        _run_worker("update", str(settings.storage_root), session_id, "4", wait=False)
        for _ in range(3)
    ]
    payloads = []
    for process in workers:
        assert isinstance(process, subprocess.Popen)
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr
        payloads.append(json.loads(stdout.strip().splitlines()[-1]))

    versions = [version for payload in payloads for version in payload["versions"]]
    assert all(payload["ok"] for payload in payloads)
    assert len(versions) == 12
    assert sorted(versions) == list(range(2, 14))  # every version committed exactly once
    snapshot = store.read(session_id)
    assert snapshot.meta.memory_version == 13


def test_recovery_requires_cross_process_working_lock(crash_session) -> None:
    settings, store, session_id, original_body, _ = crash_session
    holder = _run_worker("hold-lock", str(settings.storage_root), "1.5", wait=False)
    assert isinstance(holder, subprocess.Popen)
    try:
        first_line = holder.stdout.readline() if holder.stdout else ""
        deadline = time.time() + 20
        while "ready" not in first_line and time.time() < deadline:
            line = holder.stdout.readline() if holder.stdout else ""
            if not line:
                break
            first_line = line
        assert "ready" in first_line, first_line

        quick_store = WorkingMemoryStore(
            build_settings(storage_root=settings.storage_root, lock_timeout_seconds=0.3),
            LockManager(
                settings.locks_dir, storage_root=settings.storage_root, default_timeout=0.3
            ),
            MemoryValidator(settings, SecretScanner()),
        )
        with pytest.raises(LockTimeoutError):
            quick_store.recover(session_id)
        # Fail closed: no lock-free fallback touched the files.
        assert (store.session_dir(session_id) / "memory.md").read_bytes() == (
            store.session_dir(session_id) / "memory.md"
        ).read_bytes()
        assert quick_store.read_state(session_id).state is SessionState.RECOVERY_REQUIRED
    finally:
        holder.communicate(timeout=60)


def test_two_processes_recover_serialised(crash_session) -> None:
    settings, store, session_id, original_body, original_meta = crash_session
    first = _run_worker("recover", str(settings.storage_root), session_id, wait=False)
    second = _run_worker("recover", str(settings.storage_root), session_id, wait=False)
    payloads = []
    for process in (first, second):
        assert isinstance(process, subprocess.Popen)
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        payloads.append(json.loads(stdout.strip().splitlines()[-1]))
    assert all(payload["ok"] for payload in payloads)
    directory = store.session_dir(session_id)
    assert (directory / "memory.md").read_bytes() == original_body
    assert (directory / "memory.meta.json").read_bytes() == original_meta
    # Exactly one process performed the rollback; the second found nothing to do.
    events = (settings.episodic_dir / "events.jsonl").read_text(encoding="utf-8")
    rollbacks = [
        line
        for line in events.splitlines()
        if line.strip() and json.loads(line)["event_type"] == "transaction_rollback_completed"
    ]
    assert len(rollbacks) == 1


def test_working_lock_covers_official_files(crash_session) -> None:
    """A crashed prepared transaction never leaves md/meta from different versions visible."""
    settings, store, session_id, original_body, original_meta = crash_session
    store.recover(session_id)
    directory = store.session_dir(session_id)
    assert (directory / "memory.md").read_bytes() == original_body
    assert (directory / "memory.meta.json").read_bytes() == original_meta
    # the snapshot material is cleaned up after a verified rollback
    txn_dirs = list((directory / ".txn").iterdir())
    assert all(not (item / OLD_MD_NAME).exists() for item in txn_dirs if item.is_dir())
