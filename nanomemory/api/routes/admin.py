"""Admin endpoints: backup, verify, restore, migrations (spec §12, §22.5, §24.4, §28.2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from nanomemory.api.deps import ManagerDep, require_admin
from nanomemory.api.schemas import BackupRequest, RestoreRequest

router = APIRouter(prefix="/api/v1", tags=["admin"])

_admin = Depends(require_admin)


@router.post("/memory/backup", dependencies=[_admin])
def create_backup(body: BackupRequest, manager: ManagerDep) -> dict[str, Any]:
    manifest = manager.create_backup(label=body.label, reason="api")
    return manifest.model_dump(mode="json")


@router.get("/memory/backups", dependencies=[_admin])
def list_backups(manager: ManagerDep) -> dict[str, Any]:
    backups = manager.list_backups()
    return {
        "count": len(backups),
        "backups": [manifest.model_dump(mode="json") for manifest in backups],
    }


@router.post("/memory/backups/{backup_id}/verify", dependencies=[_admin])
def verify_backup(backup_id: str, manager: ManagerDep) -> dict[str, Any]:
    return manager.verify_backup(backup_id)


@router.post("/memory/restore", dependencies=[_admin])
def restore_backup(body: RestoreRequest, manager: ManagerDep) -> dict[str, Any]:
    """Restore a backup; requires the admin token plus an exact confirmation value."""
    result = manager.restore_backup(
        body.backup_id, confirmation=body.confirmation, force=body.force
    )
    return result.as_dict()


@router.post("/maintenance/migrate", dependencies=[_admin])
def run_migrations(manager: ManagerDep) -> dict[str, Any]:
    report = manager.run_migrations()
    return {
        "status": report.status,
        "migration_id": report.migration_id,
        "applied": report.applied,
        "from_versions": report.from_versions,
        "to_versions": report.to_versions,
        "degraded": report.degraded,
        "message": report.message,
    }


__all__ = ["router"]
