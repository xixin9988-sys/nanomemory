"""Consistent backup, verification and restore for the vector database.

SQLite is a single file, but copying it while the service is writing is not a backup: the WAL
may hold committed pages that the main file does not have yet.  This module uses SQLite's online
backup API, then hashes the result and records the counts so a restore can be verified instead of
hoped for.

Operation order for restore is deliberately conservative:

1. verify manifest hash, integrity_check and schema compatibility;
2. snapshot the current database (pre-restore file) if it exists;
3. restore into a temp file next to the target, integrity-check it, then atomic replace;
4. remove stale -wal/-shm files (the restored file is self-contained).

The service must be stopped during restore; the CLI refuses to guess.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from nanomemory.vector.db import SCHEMA_VERSION


class BackupError(RuntimeError):
    """Backup/restore refused or failed verification."""


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_id: str
    created_at: str
    sha256: str
    size_bytes: int
    schema_version: int
    memories: int
    traces: int
    feedback: int
    source_name: str


class BackupVerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    integrity: str
    sha256_matches_manifest: bool
    schema_version: int
    schema_supported: bool
    memories: int
    reason: str | None = None


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restored_from: str
    target: str
    pre_restore_backup: str | None
    memories: int
    schema_version: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def _count(connection: sqlite3.Connection, table: str) -> int:
    try:
        row = connection.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row["total"]) if row else 0


def _schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row["value"]) if row else 0


def _copy_database(source: Path, target: Path) -> None:
    """Use the SQLite online backup API so a live WAL is included consistently."""
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = _connect(source)
    dst = _connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(database: Path, destination: Path) -> BackupManifest:
    database = Path(database)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    backup_path = destination / f"vectors-{_stamp()}-{uuid.uuid4().hex[:6]}.db"
    _copy_database(database, backup_path)

    with closing(_connect(backup_path)) as connection:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "unknown"
        if integrity != "ok":
            backup_path.unlink(missing_ok=True)
            raise BackupError(f"backup integrity check failed: {integrity}")
        manifest = BackupManifest(
            backup_id=backup_path.stem,
            created_at=_now(),
            sha256=_sha256(backup_path),
            size_bytes=backup_path.stat().st_size,
            schema_version=_schema_version(connection),
            memories=_count(connection, "vector_memories"),
            traces=_count(connection, "traces"),
            feedback=_count(connection, "feedback"),
            source_name=database.name,
        )
    backup_path.with_suffix(".json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return manifest


def verify_backup(backup: Path) -> BackupVerifyResult:
    backup = Path(backup)
    if not backup.is_file():
        raise BackupError(f"backup not found: {backup}")
    manifest_path = backup.with_suffix(".json")
    sha_matches = True
    if manifest_path.is_file():
        manifest = BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        sha_matches = manifest.sha256 == _sha256(backup)
    try:
        with closing(_connect(backup)) as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0]) if integrity_row else "unknown"
            schema = _schema_version(connection)
            memories = _count(connection, "vector_memories")
    except sqlite3.DatabaseError as exc:
        return BackupVerifyResult(
            valid=False,
            integrity=f"unreadable: {type(exc).__name__}",
            sha256_matches_manifest=sha_matches,
            schema_version=0,
            schema_supported=False,
            memories=0,
            reason="database file is corrupt or truncated",
        )
    valid = integrity == "ok" and sha_matches and 0 < schema <= SCHEMA_VERSION
    reason = None
    if not sha_matches:
        reason = "sha256 does not match the manifest"
    elif integrity != "ok":
        reason = f"integrity check failed: {integrity}"
    elif schema > SCHEMA_VERSION:
        reason = f"backup schema {schema} is newer than this build ({SCHEMA_VERSION})"
    return BackupVerifyResult(
        valid=valid,
        integrity=integrity,
        sha256_matches_manifest=sha_matches,
        schema_version=schema,
        schema_supported=schema <= SCHEMA_VERSION,
        memories=memories,
        reason=reason,
    )


def restore_backup(backup: Path, database: Path, *, force: bool = False) -> RestoreResult:
    backup = Path(backup)
    database = Path(database)
    verification = verify_backup(backup)
    if not verification.valid:
        raise BackupError(f"backup is not restorable: {verification.reason}")

    pre_restore: Path | None = None
    if database.exists():
        if not force:
            raise BackupError(
                "target database exists; pass force=true to overwrite (a pre-restore snapshot "
                "will be written first)"
            )
        pre_restore = database.with_name(f"{database.stem}.pre-restore-{_stamp()}.db")
        _copy_database(database, pre_restore)

    temp = database.with_name(f"{database.stem}.restore-{uuid.uuid4().hex[:6]}.tmp")
    try:
        _copy_database(backup, temp)
        with closing(_connect(temp)) as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            if str(integrity_row[0]) != "ok":
                raise BackupError("restored copy failed integrity_check")
            memories = _count(connection, "vector_memories")
            schema = _schema_version(connection)
        os.replace(temp, database)
    finally:
        temp.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)

    return RestoreResult(
        restored_from=str(backup),
        target=str(database),
        pre_restore_backup=str(pre_restore) if pre_restore else None,
        memories=memories,
        schema_version=schema,
    )


def backup_summary(path: Path) -> dict[str, Any]:
    manifest_path = Path(path).with_suffix(".json")
    if not manifest_path.is_file():
        raise BackupError("manifest not found next to the backup")
    return BackupManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    ).model_dump()


__all__ = [
    "BackupError",
    "BackupManifest",
    "BackupVerifyResult",
    "RestoreResult",
    "backup_summary",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
