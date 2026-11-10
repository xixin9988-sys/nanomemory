"""Startup ordering and failure-state tests (spec §36.3, §36.7)."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.episodic_memory import EpisodicMemoryStore
from nanomemory.core.errors import TxnCommitFailedError
from nanomemory.core.fsio import sha256_bytes
from nanomemory.core.locks import LockManager
from nanomemory.core.memory_models import (
    MaintenanceMarker,
    MemoryUpdateProposal,
    MigrationJournal,
    SessionState,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.migrations import MigrationManager
from nanomemory.core.recovery import RecoveryEngine
from nanomemory.core.secret_scanner import SecretScanner
from nanomemory.core.sessions import SessionRegistry
from nanomemory.core.working_memory import WorkingMemoryStore


def _stack(tmp_path: Path, *, lock_timeout: float = 2.0):
    settings = build_settings(
        storage_root=tmp_path / "storage", environment="test", lock_timeout_seconds=lock_timeout
    )
    locks = LockManager(
        settings.locks_dir, storage_root=settings.storage_root, default_timeout=lock_timeout
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

    working = WorkingMemoryStore(settings, locks, validator, audit=audit)
    migrations = MigrationManager(settings, locks, audit=audit)
    registry = SessionRegistry(settings)
    registry.ensure_default()
    working.ensure_session(settings.default_session_id)
    recovery = RecoveryEngine(settings, locks, working, migrations, registry, audit=audit)
    return settings, locks, working, migrations, registry, recovery


def _write_migration_journal(settings, backup_source: Path) -> str:
    backup_dir = settings.migrations_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / "recover.json"
    backup.write_bytes(backup_source.read_bytes())
    migration_id = uuid.uuid4().hex
    journal = MigrationJournal(
        migration_id=migration_id,
        format_name="long_term",
        from_version=0,
        to_version=1,
        source_rel_path="long_term/knowledge.json",
        backup_rel_path=backup.relative_to(settings.storage_root).as_posix(),
        source_hash=sha256_bytes(backup.read_bytes()),
        status="started",
    )
    manager = MigrationManager(
        settings, LockManager(settings.locks_dir, storage_root=settings.storage_root)
    )
    manager._write_journal(journal)
    return migration_id


def test_startup_runs_migration_recovery_before_session_recovery(tmp_path, monkeypatch) -> None:
    settings, _locks, _working, migrations, _registry, recovery = _stack(tmp_path)
    knowledge = settings.long_term_dir / "knowledge.json"
    knowledge.parent.mkdir(parents=True, exist_ok=True)
    knowledge.write_text('{"schema_version": 1, "records": []}', encoding="utf-8")
    migration_id = _write_migration_journal(settings, knowledge)
    knowledge.write_text("{ half migrated", encoding="utf-8")
    migrations._write_marker(MaintenanceMarker(migration_id=migration_id))

    order: list[str] = []
    original_recover_pending = migrations.recover_pending
    original_session_recover = recovery.working.recover

    def migration_spy():
        order.append("migration")
        return original_recover_pending()

    def session_spy(session_id):
        order.append(f"session:{session_id}")
        return original_session_recover(session_id)

    monkeypatch.setattr(migrations, "recover_pending", migration_spy)
    monkeypatch.setattr(recovery.working, "recover", session_spy)

    report = recovery.startup()
    assert order[0] == "migration"
    assert any(item.startswith("session:") for item in order)
    assert report.maintenance_mode is False
    assert report.migration is not None and report.migration.status == "rolled_back"


def test_startup_rejects_session_recovery_during_maintenance_recovery(
    tmp_path, monkeypatch
) -> None:
    settings, _locks, _working, migrations, _registry, recovery = _stack(tmp_path)
    migrations._write_marker(MaintenanceMarker(migration_id=uuid.uuid4().hex))

    def degraded():
        from nanomemory.core.migrations import MigrationReport

        return MigrationReport(status="recovery_required", degraded=True, message="test")

    called: list[str] = []
    monkeypatch.setattr(migrations, "recover_pending", degraded)
    monkeypatch.setattr(recovery.working, "recover", lambda session_id: called.append(session_id))
    report = recovery.startup()
    assert report.ok is False
    assert report.maintenance_mode is True
    assert called == [], "session recovery must not run while maintenance recovery failed"
    assert migrations.maintenance_active() is True  # marker kept, service stays degraded


def test_startup_without_marker_recovers_sessions(tmp_path) -> None:
    settings, _locks, working, _migrations, _registry, recovery = _stack(tmp_path)
    session_id = settings.default_session_id
    working.update(session_id, MemoryUpdateProposal(current_goal="startup"), expected_version=1)
    report = recovery.startup()
    assert report.ok is True
    assert report.maintenance_mode is False
    assert session_id in report.sessions
    assert report.failed_sessions == []


def test_rollback_failure_releases_lock_and_marks_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, locks, working, _migrations, _registry, _recovery = _stack(tmp_path)
    session_id = settings.default_session_id

    def broken_restore(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise OSError("simulated rollback failure")

    def flaky_replace(src, dst, *args, **kwargs):  # noqa: ANN001
        if Path(dst).name == "memory.meta.json" and Path(src).name == "new_memory.meta.json":
            raise OSError(13, "replace failed")
        return original_replace(src, dst, *args, **kwargs)

    import os

    original_replace = os.replace
    monkeypatch.setattr("nanomemory.core.fsio.os.replace", flaky_replace)
    monkeypatch.setattr(working, "_restore_old_state", broken_restore)

    with pytest.raises(TxnCommitFailedError) as excinfo:
        working.update(session_id, MemoryUpdateProposal(current_goal="x"), expected_version=1)
    assert excinfo.value.details["session_state"] == "recovery_required"
    assert working.read_state(session_id).state is SessionState.RECOVERY_REQUIRED

    # The working lock must have been released: another thread can take it.
    acquired = threading.Event()

    def contender() -> None:
        other = LockManager(
            settings.locks_dir, storage_root=settings.storage_root, default_timeout=2.0
        )
        with other.acquire(["working", f"working:{session_id}"], timeout=2.0):
            acquired.set()

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(timeout=5)
    assert acquired.is_set(), "working lock was not released after rollback failure"

    # And the session refuses reads until startup recovery runs.
    from nanomemory.core.errors import NanoMemoryError

    with pytest.raises(NanoMemoryError) as read_error:
        working.read(session_id)
    assert read_error.value.code.value == "session_recovery_required"

    # Recovery material is retained: the failing transaction is still "prepared".
    txn_root = tmp_path / "storage" / "working" / session_id / ".txn"
    statuses = [
        json.loads((txn_dir / "journal.json").read_text(encoding="utf-8"))["status"]
        for txn_dir in txn_root.iterdir()
        if (txn_dir / "journal.json").is_file()
    ]
    assert "prepared" in statuses
