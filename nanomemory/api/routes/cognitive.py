"""Cognitive memory endpoints (2.0): remember, recall, explain, confirm, lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from nanomemory.api.deps import ManagerDep, SessionDep, rate_limit_dependency
from nanomemory.core.memory_models import MemoryType, SourceKind

router = APIRouter(prefix="/api/v1/memory", tags=["cognitive"])
_default_limit = rate_limit_dependency("memory", "rate_limit_per_minute")


class RememberRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    user_id: str = Field(default="local", max_length=80)
    source: str = Field(default="api", max_length=40)
    confirm: bool | None = None


class RecallResponse(BaseModel):
    query: str
    intent: str
    insufficient_context: bool
    fallback_reason: str | None
    estimated_tokens: int
    budget: int
    items: list[dict[str, Any]]


@router.post("/remember", dependencies=[Depends(_default_limit)])
def remember(
    body: RememberRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    source_kind = (
        SourceKind.USER if body.source in {"api", "user", "sdk", "cli"} else SourceKind.USER
    )
    return manager.remember(
        body.content,
        user_id=body.user_id,
        source_kind=source_kind,
        confirm=body.confirm,
        session_id=session_id,
    )


@router.get("/recall", dependencies=[Depends(_default_limit)])
def recall(
    manager: ManagerDep,
    session_id: SessionDep,
    q: str = Query(min_length=1, max_length=2_000),
    limit: int = Query(default=5, ge=1, le=50),
    user_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
) -> dict[str, Any]:
    wanted = MemoryType(memory_type) if memory_type else None
    result = manager.recall(q, session_id=session_id, user_id=user_id, memory_type=wanted)
    items = [
        {
            "id": item.id,
            "kind": item.kind,
            "content": item.content,
            "score": round(item.score, 4),
            "tokens": item.tokens,
            "reason": item.reason,
            "memory_type": item.metadata.get("memory_type"),
            "status": item.metadata.get("status"),
            "signals": item.metadata.get("signals"),
        }
        for item in result.selected
    ][:limit]
    return RecallResponse(
        query=q,
        intent=result.intent,
        insufficient_context=result.insufficient_context,
        fallback_reason=result.fallback_reason,
        estimated_tokens=result.estimated_tokens,
        budget=result.token_budget,
        items=items,
    ).model_dump(mode="json")


@router.get("/{memory_id}/explain", dependencies=[Depends(_default_limit)])
def explain(memory_id: str, manager: ManagerDep) -> dict[str, Any]:
    return manager.explain_memory(memory_id)


@router.post("/{memory_id}/confirm", dependencies=[Depends(_default_limit)])
def confirm(memory_id: str, manager: ManagerDep, session_id: SessionDep) -> dict[str, Any]:
    record = manager.confirm_memory(memory_id, session_id=session_id)
    return {"id": record.id, "status": record.status.value}


@router.post("/{memory_id}/reject", dependencies=[Depends(_default_limit)])
def reject(memory_id: str, manager: ManagerDep, session_id: SessionDep) -> dict[str, Any]:
    record = manager.reject_memory(memory_id, session_id=session_id)
    return {"id": record.id, "status": record.status.value}


@router.post("/lifecycle/run", dependencies=[Depends(_default_limit)])
def lifecycle(manager: ManagerDep, apply: bool = Query(default=False)) -> dict[str, Any]:
    return manager.run_lifecycle(dry_run=not apply)
