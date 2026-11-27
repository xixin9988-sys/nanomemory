"""Backup/restore: consistency, verification and rollback safety."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nanomemory.vector.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.schemas import MemoryFilter, MemoryMetadata, TraceLog, VectorMemoryRecord
from nanomemory.vector.store import SQLiteVectorStore
from nanomemory.vector.trace import TraceStore

TENANT = "tenant-a"
USER = "user-1"


def _record(memory_id: str) -> VectorMemoryRecord:
    return VectorMemoryRecord(
        id=memory_id,
        tenant_id=TENANT,
        user_id=USER,
        content=f"content of {memory_id}",
        metadata=MemoryMetadata(category="fact"),
        state="active",
        embedding_model="hashing-4",
        embedding_dim=4,
    )


def _build(path: Path) -> None:
    database = VectorDatabase(path)
    store = SQLiteVectorStore(database)
    store.insert(_record("vec-backup-0001"), np.array([1, 0, 0, 0], dtype=np.float32))
    store.insert(_record("vec-backup-0002"), np.array([0, 1, 0, 0], dtype=np.float32))
    TraceStore(database).record(
        TraceLog(
            trace_id="tr-backup-1",
            kind="search",
            tenant_id=TENANT,
            user_id=USER,
            query_raw="hello",
            total_latency_ms=1.0,
        )
    )
    database.close()


def _count(path: Path) -> int:
    database = VectorDatabase(path)
    try:
        return SQLiteVectorStore(database).count(MemoryFilter(tenant_id=TENANT, user_id=USER))
    finally:
        database.close()


def test_backup_roundtrip_and_manifest(tmp_path: Path) -> None:
    database_path = tmp_path / "vectors.db"
    _build(database_path)
    manifest = create_backup(database_path, tmp_path / "backups")
    assert manifest.memories == 2
    assert manifest.traces == 1
    assert manifest.schema_version >= 1
    assert manifest.size_bytes > 0

    backup_path = tmp_path / "backups" / f"{manifest.backup_id}.db"
    assert backup_path.is_file()
    result = verify_backup(backup_path)
    assert result.valid is True
    assert result.sha256_matches_manifest is True
    assert result.integrity == "ok"


def test_restore_returns_to_the_backed_up_state(tmp_path: Path) -> None:
    database_path = tmp_path / "vectors.db"
    _build(database_path)
    manifest = create_backup(database_path, tmp_path / "backups")
    backup_path = tmp_path / "backups" / f"{manifest.backup_id}.db"

    database = VectorDatabase(database_path)
    store = SQLiteVectorStore(database)
    store.insert(_record("vec-backup-0003"), np.array([0, 0, 1, 0], dtype=np.float32))
    database.close()
    assert _count(database_path) == 3

    result = restore_backup(backup_path, database_path, force=True)
    assert result.memories == 2
    assert result.pre_restore_backup is not None
    assert Path(result.pre_restore_backup).is_file()
    assert _count(database_path) == 2


def test_restore_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    database_path = tmp_path / "vectors.db"
    _build(database_path)
    manifest = create_backup(database_path, tmp_path / "backups")
    backup_path = tmp_path / "backups" / f"{manifest.backup_id}.db"
    with pytest.raises(BackupError):
        restore_backup(backup_path, database_path)


def test_tampered_backup_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "vectors.db"
    _build(database_path)
    manifest = create_backup(database_path, tmp_path / "backups")
    backup_path = tmp_path / "backups" / f"{manifest.backup_id}.db"
    data = bytearray(backup_path.read_bytes())
    data[-64:] = b"\x00" * 64
    backup_path.write_bytes(bytes(data))

    result = verify_backup(backup_path)
    assert result.valid is False
    with pytest.raises(BackupError):
        restore_backup(backup_path, database_path, force=True)


def test_backup_of_missing_database_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        create_backup(tmp_path / "missing.db", tmp_path / "backups")
