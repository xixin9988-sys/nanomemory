"""Health endpoint (spec §25.7)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from nanomemory.api.deps import ManagerDep

router = APIRouter(tags=["health"])


@router.get("/health")
def health(manager: ManagerDep) -> dict[str, Any]:
    """Service status, provider mode, degraded reasons and storage/lock probes."""
    return manager.health_dict()
