"""Token / cost / latency metrics endpoints (spec §12)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from nanomemory.api.deps import ManagerDep, rate_limit_dependency

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])
_metrics_limit = rate_limit_dependency("metrics", "rate_limit_per_minute")


@router.get("/usage", dependencies=[Depends(_metrics_limit)])
def usage(
    manager: ManagerDep,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    model: str | None = Query(default=None, max_length=120),
    provider: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    records, corrupt = manager.usage_records(
        since=since, until=until, model=model, provider=provider, limit=limit
    )
    return {
        "count": len(records),
        "records": [record.model_dump(mode="json") for record in records],
        "corrupt_lines": corrupt,
        "note": (
            "usage_source=exact only when the provider reported token usage; estimated and "
            "mock values are never billed figures."
        ),
    }


@router.get("/summary", dependencies=[Depends(_metrics_limit)])
def summary(manager: ManagerDep) -> dict[str, Any]:
    return manager.metrics_summary().as_dict()


__all__ = ["router"]
