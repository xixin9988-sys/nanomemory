"""Metrics, backup/restore, migrations and health tests (spec §22.5, §26.2, §28.2–§28.3, §30.1)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.errors import LockTimeoutError, NanoMemoryError
from nanomemory.core.health import HealthService
from nanomemory.core.locks import LockManager
from nanomemory.core.memory_models import (
    MaintenanceMarker,
    MemoryUpdateProposal,
    MigrationJournal,
    UsageRecord,
)
from nanomemory.core.metrics import MetricsStore
from nanomemory.core.migrations import MigrationManager


def _usage(index: int, *, source: str = "exact") -> UsageRecord:
    return UsageRecord(
        request_id=f"req-{index}",
        provider="mock",
        model="mock-echo",
        usage_source=source,  # type: ignore[arg-type]
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_tokens=150 if source != "exact" else None,
        latency_ms=10.0 + index,
        cost=0.0 if source == "exact" else None,
        cost_currency="USD" if source == "exact" else None,
    )


# --------------------------------------------------------------------------- metrics
def test_metrics_append_and_summary(metrics_store) -> None:
    metrics_store.record(_usage(1))
    metrics_store.record(_usage(2, source="estimated"))
    summary = metrics_store.summary()
    assert summary.requests == 2
    assert summary.exact_requests == 1
    assert summary.estimated_requests == 1
    assert summary.total_tokens_exact == 150
    assert summary.estimated_tokens_total == 150
    assert summary.by_model == {"mock-echo": 2}


def test_metrics_rotation(settings, tmp_path) -> None:
    small = build_settings(
        storage_root=tmp_path / "storage",
        metrics_max_bytes=600,
        metrics_backup_count=2,
    )
    locks = LockManager(small.locks_dir, storage_root=small.storage_root)
    store = MetricsStore(small, locks)
    store.ensure_storage()
    for index in range(12):
        store.record(_usage(index))
    rotated = list(small.metrics_dir.glob("usage.*.jsonl"))
    assert rotated, "rotation should produce rotated files"
    records, corrupt = store.read()
    assert corrupt == 0
    # Rotation keeps the newest N files; the newest record must never be lost.
    assert records
    assert records[0].request_id == "req-11"
    assert len(rotated) <= 2


def test_metrics_rotation_uses_resource_lock(settings, lock_manager) -> None:
    store = MetricsStore(settings, lock_manager)
    store.ensure_storage()
    store.record(_usage(1))
    events = [event.resource for event in lock_manager.events]
    assert "metrics" in events
    assert any(event.action == "acquire_ok" for event in lock_manager.events)


def test_metrics_corrupt_lines_isolation(metrics_store) -> None:
    metrics_store.record(_usage(1))
    with open(metrics_store.path, "a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    records, corrupt = metrics_store.read()
    assert len(records) == 1
    assert corrupt == 1
    assert metrics_store.corrupt_path.is_file()


def test_metrics_disabled_writes_nothing(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage", metrics_enabled=False)
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    store = MetricsStore(settings, locks)
    store.record(_usage(1))
    assert not store.path.exists()


# --------------------------------------------------------------------------- backup
@pytest.fixture()
def populated(working_store, prepared_session, long_term_store):
    working_store.update(
        prepared_session,
        MemoryUpdateProposal(current_goal="Backup target"),
        expected_version=1,
    )
    long_term_store.add(
        {
            "content": "backup fact",
            "category": "project",
            "source": "user",
            "source_type": "user",
            "tags": ["backup"],
        }
    )
    return prepared_session


def test_backup_creates_manifest(backup_service, populated, settings) -> None:
    manifest = backup_service.create(label="test")
    assert manifest.backup_id
    manifest_path = settings.backups_dir / manifest.backup_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["files"]
    assert all(len(entry["sha256"]) == 64 for entry in payload["files"])
    result = backup_service.verify(manifest.backup_id)
    assert result["valid"] is True


def test_backup_manifest_tamper_rejected(backup_service, populated, settings) -> None:
    manifest = backup_service.create(label="tamper")
    backup_dir = settings.backups_dir / manifest.backup_id
    target = backup_dir / "long_term" / "knowledge.json"
    data = bytearray(target.read_bytes())
    data[0] = data[0] ^ 0xFF
    target.write_bytes(bytes(data))
    assert backup_service.verify(manifest.backup_id)["valid"] is False
    with pytest.raises(NanoMemoryError) as excinfo:
        backup_service.restore(manifest.backup_id, confirmation=manifest.backup_id, force=True)
    assert excinfo.value.code.value == "backup_invalid"


def test_backup_manifest_path_traversal_rejected(backup_service, populated, settings) -> None:
    manifest = backup_service.create(label="traversal")
    manifest_path = settings.backups_dir / manifest.backup_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append(
        {
            "rel_path": "../escaped.txt",
            "sha256": "0" * 64,
            "size": 1,
            "memory_version": None,
            "schema_version": None,
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        backup_service.restore(manifest.backup_id, confirmation=manifest.backup_id, force=True)
    assert excinfo.value.code.value == "backup_invalid"


def test_restore_requires_confirmation(backup_service, populated) -> None:
    manifest = backup_service.create(label="confirm")
    with pytest.raises(NanoMemoryError) as excinfo:
        backup_service.restore(manifest.backup_id, confirmation="wrong")
    assert excinfo.value.code.value == "confirmation_required"


def test_restore_requires_force_for_version_regression(
    backup_service, populated, working_store
) -> None:
    manifest = backup_service.create(label="regress")
    working_store.update(populated, MemoryUpdateProposal(current_goal="newer"), expected_version=2)
    with pytest.raises(NanoMemoryError) as excinfo:
        backup_service.restore(manifest.backup_id, confirmation=manifest.backup_id)
    assert excinfo.value.code.value == "confirmation_required"


def test_restore_recovers_previous_state(
    backup_service, populated, working_store, settings
) -> None:
    manifest = backup_service.create(label="restore")
    working_store.update(
        populated, MemoryUpdateProposal(current_goal="diverged"), expected_version=2
    )
    result = backup_service.restore(manifest.backup_id, confirmation=manifest.backup_id, force=True)
    assert result.restored_files > 0
    snapshot = working_store.read(populated)
    assert snapshot.document.current_goal == "Backup target"
    assert snapshot.meta.memory_version == 2
    # a pre-restore snapshot must exist
    backups = backup_service.list_backups()
    assert any(item.label == "pre-restore" for item in backups)


def test_backup_unknown_id(backup_service) -> None:
    with pytest.raises(NanoMemoryError) as excinfo:
        backup_service.verify(str(uuid.uuid4()))
    assert excinfo.value.code.value == "backup_not_found"


# --------------------------------------------------------------------------- migrations
LEGACY_KNOWLEDGE = {
    "records": [
        {
            "id": "mem-legacy-1",
            "content": "Legacy record without new fields",
            "category": "legacy",
        },
        {"content": "Second legacy record"},
    ]
}


def _write_legacy(settings) -> Path:
    path = settings.long_term_dir / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(LEGACY_KNOWLEDGE, indent=2), encoding="utf-8")
    return path


def test_migration_plan_and_apply(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    path = _write_legacy(settings)
    assert manager.plan(), "legacy file must need migration"
    report = manager.migrate_all()
    assert report.status == "completed"
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 1
    assert len(migrated["records"]) == 2
    assert migrated["records"][1]["schema_version"] == 1
    assert migrated["records"][1]["source_type"] == "import"
    assert not manager.maintenance_active()
    assert manager.plan() == []
    assert manager.read_journal().status == "completed"  # type: ignore[union-attr]


def test_migration_rejects_newer_schema(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    path = settings.long_term_dir / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 99, "records": []}), encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        manager.plan()
    assert excinfo.value.code.value == "schema_unsupported"


def test_migration_lock_timeout_releases_all_acquired_locks(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root, default_timeout=0.2)
    manager = MigrationManager(settings, locks)
    path = _write_legacy(settings)
    original = path.read_text(encoding="utf-8")

    def hold() -> None:
        with locks.acquire(["metrics"]):
            time.sleep(0.6)

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.05)
    try:
        with pytest.raises(LockTimeoutError):
            manager.migrate_all()
    finally:
        thread.join(timeout=5)
    assert not manager.maintenance_active()
    assert path.read_text(encoding="utf-8") == original
    # Every lock must be released again, so a normal operation still works.
    with locks.acquire(["metrics"], timeout=1.0):
        pass


def test_migration_marker_created_only_with_all_locks(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root, default_timeout=0.2)
    manager = MigrationManager(settings, locks)
    _write_legacy(settings)
    marker_seen: list[bool] = []

    def probe() -> None:
        marker_seen.append(manager.maintenance_active())

    with manager.maintenance_scope(reason="test"):
        assert manager.maintenance_active()
        probe()
    assert marker_seen == [True]
    assert not manager.maintenance_active()


def test_migration_recovery_restores_backup(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    source = settings.long_term_dir / "knowledge.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    original_bytes = json.dumps({"schema_version": 1, "records": []}).encode()
    source.write_bytes(original_bytes)

    backup_dir = manager.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "recover-backup.json"
    backup_path.write_bytes(original_bytes)

    from nanomemory.core.fsio import sha256_bytes

    migration_id = uuid.uuid4().hex
    journal = MigrationJournal(
        migration_id=migration_id,
        format_name="long_term",
        from_version=0,
        to_version=1,
        source_rel_path="long_term/knowledge.json",
        backup_rel_path=str(backup_path.relative_to(settings.storage_root).as_posix()),
        source_hash=sha256_bytes(original_bytes),
        status="started",
    )
    manager._write_journal(journal)
    source.write_text("{ half migrated", encoding="utf-8")
    manager._write_marker(MaintenanceMarker(migration_id=migration_id))

    report = manager.recover_pending()
    assert report.status == "rolled_back"
    assert source.read_bytes() == original_bytes
    assert not manager.maintenance_active()


def test_migration_corrupt_file_refused(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    path = settings.long_term_dir / "knowledge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        manager.migrate_all()
    assert excinfo.value.code.value == "corrupt_data"
    assert path.read_text(encoding="utf-8") == "{not json"


# --------------------------------------------------------------------------- health
def test_health_ok(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    report = HealthService(settings, manager).check()
    assert report.degraded is False
    payload = report.as_dict()
    assert payload["status"] == "ok"
    assert payload["provider_mode"] == "mock"
    assert "storage" in payload["checks"]
    assert str(settings.storage_root) not in json.dumps(payload)


def test_health_degraded_without_credentials(tmp_path) -> None:
    settings = build_settings(
        storage_root=tmp_path / "storage", provider="openai_compatible", model="deepseek-chat"
    )
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    report = HealthService(settings, manager).check()
    assert report.degraded is True
    assert "provider_missing_credentials" in report.reasons


def test_health_reports_maintenance(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    manager._write_marker(MaintenanceMarker(migration_id=uuid.uuid4().hex))
    report = HealthService(settings, manager).check()
    assert report.maintenance_mode is True
    assert "maintenance_mode" in report.reasons


def test_health_degraded_when_storage_missing(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "does-not-exist")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    manager = MigrationManager(settings, locks)
    report = HealthService(settings, manager).check()
    assert report.degraded is True
    assert "storage_unwritable" in report.reasons
