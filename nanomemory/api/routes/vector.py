"""Vector memory endpoints (v2): writes, hybrid search, lifecycle, traces, feedback.

Scope is never part of a request body: it comes from the signed context envelope resolved by
the get_context dependency.  Every request model sets extra="forbid", so a client that tries
to smuggle tenant_id/user_id into JSON gets a 422 instead of a silent escalation.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from nanomemory.api.deps import (
    ContextDep,
    ManagerDep,
    RequestIdDep,
    rate_limit_dependency,
    require_admin,
)
from nanomemory.api.schemas import (
    FeedbackRequest,
    VectorCreateRequest,
    VectorRollbackRequest,
    VectorSearchRequest,
    VectorUpdateRequest,
)
from nanomemory.vector.schemas import MemoryMetadata, VectorMemoryState

router = APIRouter(prefix="/api/v1/memory/vectors", tags=["vector-memory"])
observability = APIRouter(prefix="/api/v1", tags=["observability"])
_limit = rate_limit_dependency("vector", "rate_limit_per_minute")

IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", max_length=120),
]


def _metadata_from_request(body: VectorCreateRequest) -> MemoryMetadata:
    payload = body.model_dump(mode="json", exclude_none=True, exclude={"acl_tags", "content"})
    return MemoryMetadata.model_validate(payload)


# --------------------------------------------------------------------------- writes
@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_limit)])
async def create_vector_memory(
    body: VectorCreateRequest,
    request: Request,
    response: Response,
    manager: ManagerDep,
    context: ContextDep,
    request_id: RequestIdDep,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    service = manager.require_vector()
    payload = manager.new_vector_memory(
        content=body.content,
        metadata=_metadata_from_request(body),
        acl_tags=tuple(body.acl_tags),
    )
    outcome = await service.remember(
        context, payload, idempotency_key=idempotency_key, request_id=request_id
    )
    response.headers["X-NanoMemory-Trace-Id"] = outcome.trace_id
    if outcome.idempotent_replay:
        response.headers["Idempotent-Replay"] = "true"
        response.status_code = status.HTTP_200_OK
    return {
        "record": outcome.record.to_public(),
        "trace_id": outcome.trace_id,
        "idempotent_replay": outcome.idempotent_replay,
        "embedding": (
            {
                "model": outcome.embedding.model,
                "dim": outcome.embedding.dim,
                "tokens": outcome.embedding.tokens,
            }
            if outcome.embedding
            else None
        ),
    }


@router.patch("/{memory_id}", dependencies=[Depends(_limit)])
async def update_vector_memory(
    memory_id: str,
    body: VectorUpdateRequest,
    manager: ManagerDep,
    context: ContextDep,
    request_id: RequestIdDep,
) -> dict[str, Any]:
    service = manager.require_vector()
    metadata = None
    if body.metadata is not None:
        current = service.get(context, memory_id)
        merged = current.metadata.model_dump(mode="json")
        merged.update(body.metadata.model_dump(mode="json", exclude_none=True))
        metadata = MemoryMetadata.model_validate(merged)
    outcome = await service.update(
        context,
        memory_id,
        content=body.content,
        metadata=metadata,
        acl_tags=tuple(body.acl_tags) if body.acl_tags is not None else None,
        expected_version=body.expected_version,
        request_id=request_id,
    )
    return {"record": outcome.record.to_public(), "trace_id": outcome.trace_id}


@router.post("/{memory_id}/archive", dependencies=[Depends(_limit)])
def archive_vector_memory(
    memory_id: str,
    manager: ManagerDep,
    context: ContextDep,
    expected_version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    record = manager.require_vector().archive(context, memory_id, expected_version=expected_version)
    return {"record": record.to_public()}


@router.delete("/{memory_id}", dependencies=[Depends(_limit)])
def delete_vector_memory(
    memory_id: str,
    manager: ManagerDep,
    context: ContextDep,
    expected_version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    record = manager.require_vector().delete(context, memory_id, expected_version=expected_version)
    return {"record": record.to_public()}


@router.post("/{memory_id}/reactivate", dependencies=[Depends(_limit)])
def reactivate_vector_memory(
    memory_id: str, manager: ManagerDep, context: ContextDep
) -> dict[str, Any]:
    record = manager.require_vector().reactivate(context, memory_id)
    return {"record": record.to_public()}


@router.post("/{memory_id}/retry-embedding", dependencies=[Depends(_limit)])
async def retry_vector_embedding(
    memory_id: str,
    manager: ManagerDep,
    context: ContextDep,
    request_id: RequestIdDep,
) -> dict[str, Any]:
    outcome = await manager.require_vector().retry_embedding(
        context, memory_id, request_id=request_id
    )
    return {"record": outcome.record.to_public(), "trace_id": outcome.trace_id}


@router.post("/{memory_id}/rollback", dependencies=[Depends(_limit)])
async def rollback_vector_memory(
    memory_id: str,
    body: VectorRollbackRequest,
    manager: ManagerDep,
    context: ContextDep,
    request_id: RequestIdDep,
) -> dict[str, Any]:
    outcome = await manager.require_vector().rollback(
        context, memory_id, target_version=body.target_version, request_id=request_id
    )
    return {
        "record": outcome.record.to_public(),
        "trace_id": outcome.trace_id,
        "note": "Rolled back memories stay in rolled_back until explicitly reactivated.",
    }


# --------------------------------------------------------------------------- reads
@router.post("/search", dependencies=[Depends(_limit)])
async def search_vector_memory(
    body: VectorSearchRequest,
    manager: ManagerDep,
    context: ContextDep,
    request_id: RequestIdDep,
) -> dict[str, Any]:
    result = await manager.require_vector().search(
        context,
        body.query,
        limit=body.limit,
        min_score=body.min_score,
        categories=tuple(body.categories),
        memory_types=tuple(body.memory_types),
        tags=tuple(body.tags),
        request_id=request_id,
        store_context=body.include_context,
    )
    return {
        "query": result.query,
        "trace_id": result.trace_id,
        "items": [item.model_dump(mode="json") for item in result.items],
        "candidate_count": result.candidate_count,
        "insufficient_context": result.insufficient_context,
        "fallback_reason": result.fallback_reason,
        "degraded": result.degraded,
        "embedding_model": result.embedding_model,
        "content_signal": result.content_signal,
        "timings_ms": result.timings_ms,
    }


@router.get("/stats", dependencies=[Depends(_limit)])
def vector_stats(manager: ManagerDep, context: ContextDep) -> dict[str, Any]:
    return manager.require_vector().stats(context)


@router.get("", dependencies=[Depends(_limit)])
def list_vector_memory(
    manager: ManagerDep,
    context: ContextDep,
    state: VectorMemoryState | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    records = manager.require_vector().list(
        context,
        limit=limit,
        offset=offset,
        states=(state,) if state else None,
        include_archived=include_archived,
    )
    return {
        "count": len(records),
        "records": [record.to_public() for record in records],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{memory_id}", dependencies=[Depends(_limit)])
def get_vector_memory(memory_id: str, manager: ManagerDep, context: ContextDep) -> dict[str, Any]:
    return {"record": manager.require_vector().get(context, memory_id).to_public()}


@router.get("/{memory_id}/versions", dependencies=[Depends(_limit)])
def list_vector_versions(
    memory_id: str, manager: ManagerDep, context: ContextDep
) -> dict[str, Any]:
    versions = manager.require_vector().versions(context, memory_id)
    return {
        "memory_id": memory_id,
        "count": len(versions),
        "versions": [
            {
                "version": item["version"],
                "state": item["state"],
                "created_at": item["created_at"],
                "reason": item["reason"],
                "content_preview": item["content"][:200],
            }
            for item in versions
        ],
    }


# --------------------------------------------------------------------------- feedback
@observability.post("/feedback", dependencies=[Depends(_limit)])
def submit_feedback(
    body: FeedbackRequest, manager: ManagerDep, context: ContextDep
) -> dict[str, Any]:
    record = manager.require_vector().feedback(
        context,
        trace_id=body.trace_id,
        verdict=body.verdict,
        reason=body.reason,
        comment=body.comment,
    )
    return {"feedback": record.model_dump(mode="json")}


@observability.get("/feedback", dependencies=[Depends(_limit)])
def list_feedback(manager: ManagerDep, context: ContextDep) -> dict[str, Any]:
    records = manager.require_vector().traces.list_feedback(context.tenant_id)
    return {"count": len(records), "items": [item.model_dump(mode="json") for item in records]}


@observability.get("/feedback/export", dependencies=[Depends(require_admin)])
def export_badcases(manager: ManagerDep, context: ContextDep) -> dict[str, Any]:
    """Append thumbs-down traces to the eval dataset as regression candidates."""
    path = manager.settings.badcase_export_path
    written = manager.require_vector().export_badcases(context, path)
    return {
        "exported": written,
        "path": str(path),
        "note": "Deduplicated by trace; existing cases are never rewritten.",
    }


@observability.post("/admin/traces/prune", dependencies=[Depends(require_admin)])
def prune_traces(
    manager: ManagerDep,
    context: ContextDep,
    days: int | None = Query(default=None, ge=1, le=3650),
) -> dict[str, Any]:
    """Apply the tenant's trace retention window on demand (admin only)."""
    result = manager.require_vector().prune_traces(context, days)
    return {
        **result,
        "retention_days": days or manager.settings.trace_retention_days,
        "note": "Only this tenant's traces and their feedback rows are deleted.",
    }


@observability.get("/traces/{trace_id}", dependencies=[Depends(_limit)])
def get_trace(trace_id: str, manager: ManagerDep, context: ContextDep) -> dict[str, Any]:
    trace = manager.require_vector().trace(context, trace_id)
    return trace.model_dump(mode="json")


@observability.get("/traces", dependencies=[Depends(_limit)])
def list_traces(
    manager: ManagerDep,
    context: ContextDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    traces = manager.require_vector().traces.list(context.tenant_id, limit=limit, offset=offset)
    return {
        "count": len(traces),
        "items": [
            {
                "trace_id": trace.trace_id,
                "kind": trace.kind,
                "query_raw": trace.query_raw,
                "retrieved_chunk_ids": trace.retrieved_chunk_ids,
                "insufficient_context": trace.insufficient_context,
                "degraded": trace.degraded,
                "total_latency_ms": trace.total_latency_ms,
                "created_at": trace.created_at,
            }
            for trace in traces
        ],
    }


__all__ = ["observability", "router"]
