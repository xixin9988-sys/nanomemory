"""Schema migrations with a maintenance lock and durable marker (spec §22.13, §26.2, §28.2, §34.4).

Lifecycle of a migration:

1. Acquire ``maintenance`` then every resource lock in the canonical order.  If **any**
   acquisition times out, release everything already acquired (reverse order), write no
   marker and mutate nothing.
2. Only after the full lock set is held, write the maintenance marker atomically and fsync
   its parent directory.
3. For each format that needs migration: read -> validate -> write a durable backup ->
   journal ``started`` -> transform -> verify -> atomic replace -> journal ``completed``.
4. On success clear the marker, audit and release locks (reverse order).
5. On failure roll back from the durable backup; if the rollback verifies, clear the marker,
   otherwise keep the marker, mark the service degraded and refuse normal API traffic.

Startup recovery (:meth:`MigrationManager.recover_pending`) re-checks a leftover marker under
the same full lock set, because a crash releases OS locks but leaves the marker behind.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import (
    atomic_write_json,
    atomic_write_text,
    fsync_dir,
    read_json,
    read_text,
    sha256_bytes,
    temp_name_for,
)
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import (
    SUPPORTED_SCHEMA_VERSIONS,
    MaintenanceMarker,
    MigrationJournal,
    utc_now_iso,
)
from nanomemory.core.paths import ensure_dir

AuditFn = Callable[..., None]

#: Every resource lock, in canonical order (maintenance first).
ALL_RESOURCE_LOCKS: tuple[str, ...] = (
    "maintenance",
    "backup",
    "working",
    "long_term",
    "episodic",
    "metrics",
)

LONG_TERM_FORMAT = "long_term"


@dataclass(slots=True)
class MigrationStep:
    """One ``from_version -> to_version`` transformation for a format."""

    format_name: str
    from_version: int
    to_version: int
    description: str


@dataclass(slots=True)
class MigrationReport:
    """Outcome of :meth:`MigrationManager.migrate_all`."""

    status: str
    migration_id: str | None = None
    applied: list[str] = field(default_factory=list)
    from_versions: dict[str, int] = field(default_factory=dict)
    to_versions: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    message: str | None = None


class MigrationManager:
    """Coordinates maintenance mode, schema migration and startup migration recovery."""

    def __init__(
        self,
        settings: Settings,
        locks: LockManager,
        *,
        audit: AuditFn | None = None,
    ) -> None:
        self.settings = settings
        self.locks = locks
        self._audit_fn = audit
        self.log = get_logger("migrations")
        self.marker_path = settings.maintenance_marker
        self.directory = settings.migrations_dir
        self.journal_path = self.directory / "journal.json"
        self.log_path = self.directory / "migrations.log"
        self.backup_dir = self.directory / "backup"

    # ------------------------------------------------------------------ audit
    def _audit(self, event_type: str, **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id="system", **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    # ------------------------------------------------------------------ marker
    def read_marker(self) -> MaintenanceMarker | None:
        """Return the marker, or ``None``.  A corrupt marker counts as maintenance active."""
        if not self.marker_path.is_file():
            return None
        try:
            return MaintenanceMarker.model_validate(read_json(self.marker_path))
        except Exception:  # noqa: BLE001 - corrupt marker must fail closed
            return MaintenanceMarker(
                migration_id="0" * 32,
                status="active",
                reason="marker_corrupt",
            )

    def maintenance_active(self) -> bool:
        return self.marker_path.exists()

    def assert_not_in_maintenance(self) -> None:
        """Raise ``maintenance_mode`` (503) when the marker exists."""
        if not self.maintenance_active():
            return
        marker = self.read_marker()
        raise NanoMemoryError(
            code=ErrorCode.MAINTENANCE_MODE,
            message="Service is in maintenance mode; memory operations are disabled.",
            details={"reason": marker.reason if marker else "unknown"},
        )

    def _write_marker(self, marker: MaintenanceMarker) -> None:
        ensure_dir(self.settings.storage_root, root=self.settings.storage_root, label="storage")
        result = atomic_write_json(self.marker_path, marker.model_dump(mode="json"))
        if not result.directory_fsynced:
            self.log.info("marker_fsync_unsupported", extra={"event": "marker_fsync_unsupported"})

    def _clear_marker(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            return
        fsync_dir(self.settings.storage_root)

    # ------------------------------------------------------------------ journal
    def read_journal(self) -> MigrationJournal | None:
        if not self.journal_path.is_file():
            return None
        try:
            return MigrationJournal.model_validate(read_json(self.journal_path))
        except Exception:  # noqa: BLE001
            return None

    def _write_journal(self, journal: MigrationJournal) -> None:
        ensure_dir(self.directory, root=self.settings.storage_root, label="migrations directory")
        atomic_write_json(self.journal_path, journal.model_dump(mode="json"))
        fsync_dir(self.directory)

    def _append_log(self, message: str) -> None:
        """Append a non-sensitive line to ``migrations.log``."""
        ensure_dir(self.directory, root=self.settings.storage_root, label="migrations directory")
        line = f"{utc_now_iso()} {message}\n"
        with open(self.log_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    # ------------------------------------------------------------------ planning
    def current_versions(self) -> dict[str, int]:
        version = 0
        path = self.settings.long_term_dir / "knowledge.json"
        if path.is_file():
            try:
                payload = read_json(path)
                version = int(payload.get("schema_version", 0))
            except NanoMemoryError:
                version = -1  # corrupt: surfaced by the migration itself
        return {LONG_TERM_FORMAT: version}

    def plan(self) -> list[MigrationStep]:
        steps: list[MigrationStep] = []
        versions = self.current_versions()
        current = versions.get(LONG_TERM_FORMAT, 0)
        target = SUPPORTED_SCHEMA_VERSIONS["long_term"]
        if current > target:
            raise NanoMemoryError(
                code=ErrorCode.SCHEMA_UNSUPPORTED,
                message="Long-term memory schema is newer than this build supports.",
                details={"found": current, "supported": target},
            )
        if current == -1:
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="Long-term memory file cannot be read; refusing to migrate it.",
            )
        if current < 1:
            steps.append(
                MigrationStep(
                    format_name=LONG_TERM_FORMAT,
                    from_version=current,
                    to_version=1,
                    description="normalise legacy knowledge.json to schema v1",
                )
            )
        if current < 2:
            steps.append(
                MigrationStep(
                    format_name=LONG_TERM_FORMAT,
                    from_version=max(current, 1),
                    to_version=2,
                    description="add cognitive fields (memory_type, provenance, validity, supersede)",
                )
            )
        return steps

    # ------------------------------------------------------------------ migrate
    def migrate_all(self) -> MigrationReport:
        """Run every pending migration inside maintenance mode."""
        steps = self.plan()
        if not steps:
            return MigrationReport(status="up_to_date", applied=[])
        migration_id = uuid.uuid4().hex
        versions = self.current_versions()
        marker = MaintenanceMarker(
            migration_id=migration_id,
            from_versions=versions,
            pid=os.getpid(),
        )
        applied: list[str] = []
        with self.locks.acquire(list(ALL_RESOURCE_LOCKS), operation="migration"):
            # Marker only after every lock is held (spec §34.4).
            self._write_marker(marker)
            self._audit("migration_started", migration_id=migration_id, from_versions=versions)
            try:
                for step in steps:
                    self._apply_step(step, migration_id)
                    applied.append(f"{step.format_name}:{step.from_version}->{step.to_version}")
            except Exception as exc:
                self._audit(
                    "migration_failed",
                    migration_id=migration_id,
                    reason=type(exc).__name__,
                )
                self._append_log(f"migration {migration_id} failed: {type(exc).__name__}")
                journal = self.read_journal()
                restored = True if journal is None else self._restore_from_journal(journal)
                if restored:
                    if journal is not None:
                        journal.status = "rolled_back"
                        journal.completed_at = utc_now_iso()
                        self._write_journal(journal)
                    self._clear_marker()
                    self._audit("migration_rollback_completed", migration_id=migration_id)
                else:
                    # Marker stays: the service must remain in maintenance mode until a
                    # human (or the startup recovery path) verifies the data.
                    self._audit(
                        "migration_recovery_required",
                        migration_id=migration_id,
                        reason="rollback_unverified",
                    )
                raise
            self._clear_marker()
            self._audit("migration_completed", migration_id=migration_id, applied=applied)
            self._append_log(f"migration {migration_id} completed: {applied}")
        return MigrationReport(
            status="completed",
            migration_id=migration_id,
            applied=applied,
            from_versions=versions,
            to_versions=self.current_versions(),
        )

    def _apply_step(self, step: MigrationStep, migration_id: str) -> None:
        if step.format_name != LONG_TERM_FORMAT:
            raise NanoMemoryError(
                code=ErrorCode.MIGRATION_FAILED,
                message=f"No migration implementation for format {step.format_name}.",
            )
        path = self.settings.long_term_dir / "knowledge.json"
        original_bytes = path.read_bytes()
        source_hash = sha256_bytes(original_bytes)
        payload = json.loads(original_bytes.decode("utf-8"))

        ensure_dir(self.backup_dir, root=self.settings.storage_root, label="migration backup")
        backup_path = self.backup_dir / f"{migration_id}-knowledge-v{step.from_version}.json"
        atomic_write_text(backup_path, original_bytes.decode("utf-8"))

        journal = MigrationJournal(
            migration_id=migration_id,
            format_name=step.format_name,
            from_version=step.from_version,
            to_version=step.to_version,
            source_rel_path=path.relative_to(self.settings.storage_root).as_posix(),
            backup_rel_path=backup_path.relative_to(self.settings.storage_root).as_posix(),
            source_hash=source_hash,
        )
        self._write_journal(journal)

        migrated = self._transform_long_term(payload, step.to_version)
        target_bytes = (
            json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
        atomic_write_text(path, target_bytes.decode("utf-8"))
        verified = json.loads(read_text(path))
        if int(verified.get("schema_version", 0)) != step.to_version:
            raise NanoMemoryError(
                code=ErrorCode.MIGRATION_FAILED, message="migration verification failed"
            )

        journal.status = "completed"
        journal.target_hash = sha256_bytes(target_bytes)
        journal.completed_at = utc_now_iso()
        self._write_journal(journal)

    def _transform_long_term(self, payload: dict[str, Any], to_version: int) -> dict[str, Any]:
        """Normalise legacy records to ``to_version``; never invents semantic content."""
        if not isinstance(payload, dict):
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="knowledge.json root must be an object.",
            )
        raw_records = payload.get("records", [])
        if not isinstance(raw_records, list):
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA, message="knowledge.json records must be a list."
            )
        records: list[dict[str, Any]] = []
        for index, record in enumerate(raw_records):
            if not isinstance(record, dict):
                raise NanoMemoryError(
                    code=ErrorCode.CORRUPT_DATA,
                    message="each long-term record must be an object.",
                )
            normalised = dict(record)
            normalised.setdefault("id", f"mem-legacy-{index:06d}")
            normalised.setdefault("content", "")
            normalised.setdefault("category", "general")
            normalised.setdefault("created_at", utc_now_iso())
            normalised.setdefault("updated_at", normalised["created_at"])
            normalised.setdefault("source", "legacy-import")
            normalised.setdefault("source_type", "import")
            normalised.setdefault("confidence", "medium")
            normalised.setdefault("importance", 3)
            normalised.setdefault("tags", [])
            normalised.setdefault("status", "active")
            normalised.setdefault("trust_level", "untrusted")
            normalised.setdefault("version", 1)
            normalised.setdefault("related_ids", [])
            if to_version >= 2:
                self._upgrade_record_to_v2(normalised)
            normalised["schema_version"] = to_version
            records.append(normalised)
        return {
            "schema_version": to_version,
            "updated_at": utc_now_iso(),
            "records": records,
        }

    @staticmethod
    def _upgrade_record_to_v2(record: dict[str, Any]) -> None:
        """Add cognitive fields; derive type/provenance from existing metadata only."""
        category = str(record.get("category", "")).lower()
        if any(marker in category for marker in ("preference", "prefer", "偏好")):
            memory_type = "preference"
        elif any(marker in category for marker in ("procedure", "howto", "workflow", "流程")):
            memory_type = "procedure"
        elif any(marker in category for marker in ("relationship", "relation", "关系")):
            memory_type = "relationship"
        elif any(marker in category for marker in ("autobiograph", "biography", "profile")):
            memory_type = "autobiographical"
        else:
            memory_type = "fact"
        record.setdefault("memory_type", memory_type)
        record.setdefault("user_id", None)
        record.setdefault("valid_from", record.get("created_at"))
        record.setdefault(
            "valid_until", None if record.get("status") == "active" else record.get("updated_at")
        )
        source_type = str(record.get("source_type", "import"))
        source_kind = {
            "user": "user",
            "model": "model",
            "tool": "tool",
            "system": "file",
            "import": "import",
        }.get(source_type, "unknown")
        record.setdefault(
            "provenance",
            {
                "source_kind": source_kind,
                "source_ref": record.get("source"),
                "derived_from": [],
                "created_by": "migration-v1-v2",
                "note": None,
            },
        )
        record.setdefault("supersedes", [])
        record.setdefault("superseded_by", None)
        record.setdefault("last_recalled_at", None)
        record.setdefault("recall_count", 0)

    # ------------------------------------------------------------------ recovery
    def recover_pending(self) -> MigrationReport:
        """Recover a migration that crashed after its marker became durable (spec §34.4)."""
        marker = self.read_marker()
        if marker is None:
            return MigrationReport(status="no_marker")
        try:
            with self.locks.acquire(list(ALL_RESOURCE_LOCKS), operation="migration_recovery"):
                journal = self.read_journal()
                if journal is None:
                    # Marker without a journal: nothing was started, so removal is safe.
                    self._clear_marker()
                    self._audit("migration_rollback_completed", migration_id=marker.migration_id)
                    return MigrationReport(
                        status="cleared_without_journal", migration_id=marker.migration_id
                    )
                if journal.status == "completed":
                    self._clear_marker()
                    self._audit(
                        "migration_completed", migration_id=marker.migration_id, recovered=True
                    )
                    return MigrationReport(status="completed", migration_id=marker.migration_id)
                restored = self._restore_from_journal(journal)
                if not restored:
                    self._audit(
                        "migration_recovery_required",
                        migration_id=marker.migration_id,
                        reason="backup_missing_or_hash_mismatch",
                    )
                    return MigrationReport(
                        status="recovery_required",
                        migration_id=marker.migration_id,
                        degraded=True,
                        message="migration backup missing or unverifiable; marker kept",
                    )
                self._clear_marker()
                self._audit("migration_rollback_completed", migration_id=marker.migration_id)
                return MigrationReport(status="rolled_back", migration_id=marker.migration_id)
        except NanoMemoryError as exc:
            if exc.code is ErrorCode.LOCK_TIMEOUT:
                return MigrationReport(
                    status="lock_timeout",
                    migration_id=marker.migration_id,
                    degraded=True,
                    message="could not acquire migration recovery locks",
                )
            raise

    def _restore_from_journal(self, journal: MigrationJournal) -> bool:
        if not journal.backup_rel_path:
            return False
        backup = self.settings.storage_root / journal.backup_rel_path
        target = self.settings.storage_root / journal.source_rel_path
        if not backup.is_file():
            return False
        backup_bytes = backup.read_bytes()
        if journal.source_hash and sha256_bytes(backup_bytes) != journal.source_hash:
            return False
        temp = target.parent / temp_name_for(target)
        try:
            temp.write_bytes(backup_bytes)
            os.replace(temp, target)
            fsync_dir(target.parent)
        except OSError:  # pragma: no cover - disk/permission failure
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return sha256_bytes(target.read_bytes()) == (journal.source_hash or "")

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def maintenance_scope(self, *, reason: str = "manual_maintenance") -> Iterator[str]:
        """Context manager that holds every lock and a marker for a maintenance operation."""
        migration_id = uuid.uuid4().hex
        marker = MaintenanceMarker(migration_id=migration_id, reason=reason, pid=os.getpid())
        with self.locks.acquire(list(ALL_RESOURCE_LOCKS), operation=reason):
            self._write_marker(marker)
            try:
                yield migration_id
            finally:
                self._clear_marker()

    def status(self) -> dict[str, Any]:
        marker = self.read_marker()
        return {
            "maintenance_active": marker is not None,
            "marker": marker.model_dump(mode="json") if marker else None,
            "current_versions": self.current_versions(),
            "supported_versions": dict(SUPPORTED_SCHEMA_VERSIONS),
        }


__all__ = [
    "ALL_RESOURCE_LOCKS",
    "LONG_TERM_FORMAT",
    "MigrationManager",
    "MigrationReport",
    "MigrationStep",
]
