"""Backup, verification and restore (spec §22.5, §24.2, §28.3).

* Every backup has a UUID ``backup_id`` and a ``manifest.json`` with relative paths,
  per-file SHA-256, sizes, versions and schema versions.
* Restore validates the manifest, the paths (no traversal, no absolute paths, no symlinks)
  and every file hash **before** touching storage.  A single changed byte makes the restore
  fail (``test_backup_manifest_tamper_rejected``).
* Restore takes the ``backup`` lock, then the resource locks in canonical order, writes a
  pre-restore snapshot directly through its own helper (never via ``create_backup``), checks
  schema compatibility, requires an explicit confirmation string and, when a version would
  regress, additionally requires ``force=True``.
* Restore writes ``restore_started`` / ``restore_completed`` / ``restore_failed`` audit
  events through the injected audit sink.  If the post-restore verification fails, recovery
  is attempted from the pre-restore snapshot; a failed recovery leaves the maintenance marker
  in place so the service fails closed.
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import (
    atomic_write_bytes,
    atomic_write_json,
    fsync_dir,
    read_bytes,
    read_json,
    sha256_bytes,
)
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import (
    BACKUP_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    BackupFileEntry,
    BackupManifest,
    MaintenanceMarker,
)
from nanomemory.core.paths import ensure_dir, find_symlink_component, is_link_like

AuditFn = Callable[..., None]

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

RESTORE_LOCKS: tuple[str, ...] = ("backup", "working", "long_term", "episodic", "metrics")
CREATE_LOCKS: tuple[str, ...] = ("backup", "working", "long_term", "episodic", "metrics")


@dataclass(slots=True)
class RestoreResult:
    """Outcome of a successful restore."""

    backup_id: str
    restored_files: int
    pre_restore_backup_id: str | None = None
    forced: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "restored_files": self.restored_files,
            "pre_restore_backup_id": self.pre_restore_backup_id,
            "forced": self.forced,
            "warnings": self.warnings,
        }


class BackupService:
    """Consistent snapshots of the memory storage tree plus verified restores."""

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
        self.log = get_logger("backup")
        self.root = settings.storage_root
        self.directory = settings.backups_dir

    # ------------------------------------------------------------------ audit
    def _audit(self, event_type: str, *, session_id: str = "system", **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id=session_id, **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    # ------------------------------------------------------------------ discovery
    def _backup_path(self, backup_id: str) -> Path:
        if not UUID4_RE.match(backup_id):
            raise NanoMemoryError(
                code=ErrorCode.BACKUP_INVALID,
                message="Backup id must be a UUID4 string.",
            )
        return self.directory / backup_id.lower()

    def list_backups(self) -> list[BackupManifest]:
        if not self.directory.is_dir():
            return []
        manifests: list[BackupManifest] = []
        for entry in sorted(self.directory.iterdir(), reverse=True):
            if not entry.is_dir() or is_link_like(entry):
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifests.append(BackupManifest.model_validate(read_json(manifest_path)))
            except Exception:  # noqa: BLE001 - list is best effort; verify() is strict
                continue
        return manifests

    def _source_files(self) -> list[Path]:
        """Files that constitute the memory payload (never includes backups themselves)."""
        files: list[Path] = []
        long_term = self.settings.long_term_dir / "knowledge.json"
        if long_term.is_file():
            files.append(long_term)
        for name in ("events.jsonl", "quarantine_acks.json"):
            candidate = self.settings.episodic_dir / name
            if candidate.is_file():
                files.append(candidate)
        metrics = self.settings.metrics_dir / "usage.jsonl"
        if metrics.is_file():
            files.append(metrics)
        working_root = self.settings.working_dir
        if working_root.is_dir():
            for session_dir in sorted(working_root.iterdir()):
                if not session_dir.is_dir() or is_link_like(session_dir):
                    continue
                for name in ("memory.md", "memory.meta.json", ".session.json", ".state.json"):
                    candidate = session_dir / name
                    if candidate.is_file() and not is_link_like(candidate):
                        files.append(candidate)
        return files

    # ------------------------------------------------------------------ create
    def create(self, *, label: str = "manual", reason: str = "manual") -> BackupManifest:
        """Create a consistent backup under the backup lock."""
        with self.locks.acquire(list(CREATE_LOCKS), operation=f"backup_create:{reason}"):
            return self._create_locked(label=label, reason=reason)

    def _create_locked(self, *, label: str, reason: str) -> BackupManifest:
        self._reject_unsafe_directories()
        backup_id = str(uuid.uuid4())
        target_dir = ensure_dir(
            self.directory / backup_id, root=self.root, label="backup directory"
        )
        entries: list[BackupFileEntry] = []
        schema_versions: dict[str, int] = {}
        session_ids: list[str] = []
        for source in sorted(self._source_files()):
            rel = source.resolve().relative_to(self.root.resolve()).as_posix()
            if find_symlink_component(self.root, source) is not None:
                continue
            data = read_bytes(source)
            digest = sha256_bytes(data)
            entries.append(
                BackupFileEntry(
                    rel_path=rel,
                    sha256=digest,
                    size=len(data),
                    memory_version=self._memory_version(source),
                    schema_version=self._schema_version(source, schema_versions),
                )
            )
            destination = target_dir / rel
            ensure_dir(destination.parent, root=self.root, label="backup subdirectory")
            atomic_write_bytes(destination, data)
            if rel.startswith("working/"):
                session_ids.append(rel.split("/")[1])
        manifest = BackupManifest(
            backup_id=backup_id,
            label=label,
            product_version=self.settings.product_version,
            schema_versions=schema_versions
            or {"long_term": SUPPORTED_SCHEMA_VERSIONS["long_term"]},
            files=entries,
            session_ids=sorted(set(session_ids)),
        )
        atomic_write_json(target_dir / "manifest.json", manifest.model_dump(mode="json"))
        self._audit(
            "backup_created",
            backup_id=backup_id,
            files=len(entries),
            label=label,
            reason=reason,
        )
        self._prune_locked(keep_new=manifest.backup_id)
        return manifest

    # ------------------------------------------------------------------ verify
    def verify(self, backup_id: str) -> dict[str, Any]:
        """Validate a backup's own files and hashes without modifying storage."""
        manifest = self._load_manifest(backup_id)
        self._validate_manifest_paths(manifest)
        backup_dir = self._backup_path(manifest.backup_id)
        problems: list[dict[str, str]] = []
        for entry in manifest.files:
            path = backup_dir / entry.rel_path
            if not path.is_file():
                problems.append({"path": entry.rel_path, "problem": "missing"})
                continue
            data = read_bytes(path)
            if len(data) != entry.size or sha256_bytes(data) != entry.sha256:
                problems.append({"path": entry.rel_path, "problem": "hash_mismatch"})
        return {
            "backup_id": manifest.backup_id,
            "valid": not problems,
            "files": len(manifest.files),
            "problems": problems,
            "created_at": manifest.created_at,
            "schema_versions": manifest.schema_versions,
        }

    def _load_manifest(self, backup_id: str) -> BackupManifest:
        path = self._backup_path(backup_id) / "manifest.json"
        if not path.is_file():
            raise NanoMemoryError(
                code=ErrorCode.BACKUP_NOT_FOUND,
                message="Backup not found.",
                details={"backup_id": backup_id},
            )
        try:
            manifest = BackupManifest.model_validate(read_json(path))
        except NanoMemoryError:
            raise
        except Exception as exc:
            raise NanoMemoryError(
                code=ErrorCode.BACKUP_INVALID,
                message="Backup manifest is invalid.",
                details={"reason": str(exc).splitlines()[0][:200]},
            ) from exc
        if manifest.schema_version > BACKUP_SCHEMA_VERSION:
            raise NanoMemoryError(
                code=ErrorCode.SCHEMA_UNSUPPORTED,
                message="Backup manifest schema is newer than this build supports.",
            )
        return manifest

    def _validate_manifest_paths(self, manifest: BackupManifest) -> None:
        root = self.root.resolve()
        for entry in manifest.files:
            rel = Path(entry.rel_path)
            if rel.is_absolute() or ".." in rel.parts:
                raise NanoMemoryError(
                    code=ErrorCode.BACKUP_INVALID,
                    message="Backup manifest contains an illegal path.",
                    details={"path": entry.rel_path},
                )
            resolved = (root / rel).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise NanoMemoryError(
                    code=ErrorCode.BACKUP_INVALID,
                    message="Backup manifest path escapes the storage root.",
                    details={"path": entry.rel_path},
                ) from exc

    # ------------------------------------------------------------------ restore
    def restore(
        self,
        backup_id: str,
        *,
        confirmation: str,
        force: bool = False,
        restore_session_ids: list[str] | None = None,
    ) -> RestoreResult:
        """Restore a backup; ``confirmation`` must repeat the backup id exactly."""
        if confirmation.strip() != backup_id.strip():
            raise NanoMemoryError(
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="Restore requires a confirmation value equal to the backup id.",
            )
        manifest = self._load_manifest(backup_id)
        self._validate_manifest_paths(manifest)
        for name, version in manifest.schema_versions.items():
            supported = SUPPORTED_SCHEMA_VERSIONS.get(name)
            if supported is not None and version > supported:
                raise NanoMemoryError(
                    code=ErrorCode.SCHEMA_UNSUPPORTED,
                    message=f"Backup schema for '{name}' is newer than this build supports.",
                    details={"found": version, "supported": supported},
                )

        with self.locks.acquire(list(RESTORE_LOCKS), operation="restore"):
            self._assert_no_pending_transactions()
            self._verify_backup_files(manifest)
            self._check_version_regression(manifest, force=force)
            self._audit(
                "restore_started",
                backup_id=backup_id,
                files=len(manifest.files),
                forced=force,
            )
            pre = self._create_locked(label="pre-restore", reason="restore")
            try:
                restored = self._apply_manifest(manifest)
                self._verify_restored_files(manifest)
            except Exception as exc:
                recovered = self._recover_from_snapshot(pre, manifest)
                self._audit(
                    "restore_failed",
                    backup_id=backup_id,
                    recovered=recovered,
                    reason=type(exc).__name__,
                )
                if not recovered:
                    self._enter_maintenance(reason="restore_recovery_failed")
                raise NanoMemoryError(
                    code=ErrorCode.RESTORE_FAILED,
                    message="Restore failed; pre-restore snapshot was used for recovery.",
                    details={"backup_id": backup_id, "recovered": recovered},
                ) from exc
            self._audit("restore_completed", backup_id=backup_id, files=restored)
            return RestoreResult(
                backup_id=backup_id,
                restored_files=restored,
                pre_restore_backup_id=pre.backup_id,
                forced=force,
            )

    def _assert_no_pending_transactions(self) -> None:
        working_root = self.settings.working_dir
        if not working_root.is_dir():
            return
        for session_dir in working_root.iterdir():
            if not session_dir.is_dir():
                continue
            state_file = session_dir / ".state.json"
            if state_file.is_file():
                try:
                    state = read_json(state_file).get("state")
                except Exception:  # noqa: BLE001
                    state = "unknown"
                if state not in {"active", None}:
                    raise NanoMemoryError(
                        code=ErrorCode.RESTORE_FAILED,
                        message="A session requires recovery; complete it before restoring.",
                        details={"session_id": session_dir.name, "state": state},
                    )
            txn_root = session_dir / ".txn"
            if txn_root.is_dir():
                for txn_dir in txn_root.iterdir():
                    journal_path = txn_dir / "journal.json"
                    if not journal_path.is_file():
                        continue
                    try:
                        journal = read_json(journal_path)
                    except Exception:  # noqa: BLE001 - unreadable journal needs recovery
                        raise NanoMemoryError(
                            code=ErrorCode.RESTORE_FAILED,
                            message="A Working Memory journal is unreadable; recover first.",
                            details={"session_id": session_dir.name},
                        ) from None
                    if journal.get("status") == "prepared":
                        raise NanoMemoryError(
                            code=ErrorCode.RESTORE_FAILED,
                            message="An unfinished Working Memory transaction exists.",
                            details={
                                "session_id": session_dir.name,
                                "txn_id": journal.get("txn_id"),
                            },
                        )

    def _verify_backup_files(self, manifest: BackupManifest) -> None:
        backup_dir = self._backup_path(manifest.backup_id)
        for entry in manifest.files:
            source = backup_dir / entry.rel_path
            if not source.is_file():
                raise NanoMemoryError(
                    code=ErrorCode.BACKUP_INVALID,
                    message="Backup file is missing.",
                    details={"path": entry.rel_path},
                )
            data = read_bytes(source)
            if len(data) != entry.size or sha256_bytes(data) != entry.sha256:
                raise NanoMemoryError(
                    code=ErrorCode.BACKUP_INVALID,
                    message="Backup file failed integrity verification.",
                    details={"path": entry.rel_path},
                )

    def _check_version_regression(self, manifest: BackupManifest, *, force: bool) -> None:
        if force:
            return
        for entry in manifest.files:
            if entry.memory_version is None:
                continue
            current = self._memory_version(self.root / entry.rel_path)
            if current is not None and current > entry.memory_version:
                raise NanoMemoryError(
                    code=ErrorCode.CONFIRMATION_REQUIRED,
                    message="Restore would move a session version backwards; pass force=true.",
                    details={
                        "path": entry.rel_path,
                        "current_version": current,
                        "backup_version": entry.memory_version,
                    },
                )

    def _verify_restored_files(self, manifest: BackupManifest) -> None:
        """Confirm storage now matches the manifest byte-for-byte."""
        for entry in manifest.files:
            destination = self.root / entry.rel_path
            if not destination.is_file():
                raise NanoMemoryError(
                    code=ErrorCode.RESTORE_FAILED,
                    message="Restored file is missing.",
                    details={"path": entry.rel_path},
                )
            data = read_bytes(destination)
            if len(data) != entry.size or sha256_bytes(data) != entry.sha256:
                raise NanoMemoryError(
                    code=ErrorCode.RESTORE_FAILED,
                    message="Restored file failed verification.",
                    details={"path": entry.rel_path},
                )

    def _apply_manifest(self, manifest: BackupManifest) -> int:
        backup_dir = self._backup_path(manifest.backup_id)
        restored = 0
        for entry in manifest.files:
            source = backup_dir / entry.rel_path
            destination = self.root / entry.rel_path
            ensure_dir(destination.parent, root=self.root, label=f"restore target {entry.rel_path}")
            atomic_write_bytes(destination, read_bytes(source))
            restored += 1
        fsync_dir(self.root)
        return restored

    def _recover_from_snapshot(
        self, snapshot: BackupManifest, failed_manifest: BackupManifest
    ) -> bool:
        """Restore the pre-restore snapshot for the files the failed restore touched."""
        backup_dir = self._backup_path(snapshot.backup_id)
        try:
            for entry in snapshot.files:
                source = backup_dir / entry.rel_path
                if not source.is_file() or sha256_bytes(read_bytes(source)) != entry.sha256:
                    return False
                destination = self.root / entry.rel_path
                ensure_dir(destination.parent, root=self.root, label="restore rollback target")
                atomic_write_bytes(destination, read_bytes(source))
            # Files that exist only in the failed backup but not in the snapshot cannot be
            # rolled back automatically; report failure instead of guessing.
            snapshot_paths = {entry.rel_path for entry in snapshot.files}
            extra = [
                entry.rel_path
                for entry in failed_manifest.files
                if entry.rel_path not in snapshot_paths
            ]
            if extra:
                self.log.error(
                    "restore_rollback_incomplete",
                    extra={"event": "restore_rollback_incomplete", "count": len(extra)},
                )
                return False
            return True
        except Exception:  # noqa: BLE001 - rollback failure is reported, not masked
            return False

    def _enter_maintenance(self, *, reason: str) -> None:
        marker = MaintenanceMarker(
            migration_id=uuid.uuid4().hex,
            status="active",
            reason=reason,
        )
        try:
            atomic_write_json(self.settings.maintenance_marker, marker.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - degrade silently is worse than crashing?
            self.log.error("maintenance_marker_write_failed", extra={"event": "marker_failed"})

    # ------------------------------------------------------------------ retention
    def _prune_locked(self, *, keep_new: str | None = None) -> None:
        manifests = [m for m in self.list_backups() if m.backup_id != keep_new]
        manifests.sort(key=lambda item: item.created_at, reverse=True)
        keep: set[str] = set()
        for index, manifest in enumerate(manifests):
            if index < self.settings.backup_retention_count:
                keep.add(manifest.backup_id)
        daily_seen: set[str] = set()
        for manifest in manifests:
            day = manifest.created_at[:10]
            if day in daily_seen:
                continue
            if len(daily_seen) < self.settings.backup_retention_daily:
                daily_seen.add(day)
                keep.add(manifest.backup_id)
        for manifest in manifests:
            if manifest.backup_id in keep:
                continue
            target = self._backup_path(manifest.backup_id)
            try:
                shutil.rmtree(target)
            except OSError:  # pragma: no cover - retention is best effort
                continue
            self._audit("backup_pruned", backup_id=manifest.backup_id)

    # ------------------------------------------------------------------ helpers
    def _reject_unsafe_directories(self) -> None:
        for path in (
            self.settings.storage_root,
            self.settings.long_term_dir,
            self.settings.episodic_dir,
        ):
            if path.exists() and is_link_like(path):
                raise NanoMemoryError(
                    code=ErrorCode.SYMLINK_DETECTED,
                    message="Storage directory is a symlink/junction; refusing to back it up.",
                )

    @staticmethod
    def _memory_version(path: Path) -> int | None:
        if path.name != "memory.meta.json" or not path.is_file():
            return None
        try:
            return int(read_json(path).get("memory_version"))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _schema_version(path: Path, accumulator: dict[str, int]) -> int | None:
        try:
            if path.name == "knowledge.json":
                version = int(read_json(path).get("schema_version", 0))
                accumulator["long_term"] = version
                return version
            if path.name == "memory.meta.json":
                version = int(read_json(path).get("schema_version", 0))
                accumulator.setdefault("working_meta", version)
                return version
        except Exception:  # noqa: BLE001
            return None
        return None

    def statistics(self) -> dict[str, Any]:
        backups = self.list_backups()
        return {
            "backups": len(backups),
            "latest": backups[0].backup_id if backups else None,
            "retention_count": self.settings.backup_retention_count,
            "retention_daily": self.settings.backup_retention_daily,
        }


__all__ = ["BackupService", "RESTORE_LOCKS", "RestoreResult", "UUID4_RE"]
