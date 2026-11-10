"""Memory endpoints: Working Memory, Long-term Memory, Episodes and capacity (spec §12)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from nanomemory.api.deps import (
    ManagerDep,
    SessionDep,
    rate_limit_dependency,
    require_admin,
)
from nanomemory.api.schemas import (
    CompressRequest,
    DeleteResponse,
    EpisodeCreateRequest,
    LongTermCreateRequest,
    LongTermUpdateRequest,
    QuarantineAcknowledgeRequest,
    WorkingMemoryProposalRequest,
)
from nanomemory.core.episodic_memory import EpisodicQuery
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.memory_models import MemoryStatus, MemoryUpdateProposal

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])
_default_limit = rate_limit_dependency("memory", "rate_limit_per_minute")


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "session_id": snapshot.session_id,
        "memory_version": snapshot.meta.memory_version,
        "updated_at": snapshot.meta.updated_at,
        "content_sha256": snapshot.meta.content_sha256,
        "status": snapshot.meta.status.value,
        "document": snapshot.document.model_dump(mode="json"),
        "markdown": snapshot.markdown,
    }


def _record_payload(record: Any) -> dict[str, Any]:
    return record.model_dump(mode="json")


# --------------------------------------------------------------------------- working
@router.get("/working", dependencies=[Depends(_default_limit)])
def get_working_memory(manager: ManagerDep, session_id: SessionDep) -> dict[str, Any]:
    return _snapshot_payload(manager.read_working_memory(session_id))


@router.put("/working", dependencies=[Depends(_default_limit)])
def put_working_memory(
    body: WorkingMemoryProposalRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    """Validate and commit a Working Memory proposal with optimistic concurrency."""
    proposal = MemoryUpdateProposal(
        current_goal=body.current_goal,
        status=body.status,
        relevant_context=body.relevant_context,
        completed=body.completed,
        in_progress=body.in_progress,
        blockers=body.blockers,
        next_action=body.next_action,
        allow_task_removal=body.allow_task_removal,
    )
    result = manager.apply_memory_update(
        session_id, proposal, expected_version=body.expected_version
    )
    return {
        "memory_version": result.snapshot.meta.memory_version,
        "txn_id": result.txn_id,
        "secret_hits": result.secret_hits,
        "snapshot": _snapshot_payload(result.snapshot),
    }


@router.post("/compress", dependencies=[Depends(_default_limit)])
def compress_working_memory(
    body: CompressRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    return manager.compress_working_memory(session_id, mode=body.mode, apply=body.apply)


# --------------------------------------------------------------------------- long term
@router.get("/long-term", dependencies=[Depends(_default_limit)])
def list_long_term(
    manager: ManagerDep,
    category: str | None = Query(default=None, max_length=80),
    tag: str | None = Query(default=None, max_length=40),
    status: MemoryStatus | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    records = manager.list_long_term_memory(
        category=category,
        tag=tag,
        status=status,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return {
        "count": len(records),
        "records": [_record_payload(record) for record in records],
        "capacity": manager.long_term.capacity(),
    }


@router.post("/long-term", dependencies=[Depends(_default_limit)])
def create_long_term(
    body: LongTermCreateRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude_none=True)
    record = manager.add_long_term_memory(payload, session_id=session_id)
    return _record_payload(record)


@router.get("/long-term/{memory_id}", dependencies=[Depends(_default_limit)])
def get_long_term(memory_id: str, manager: ManagerDep) -> dict[str, Any]:
    return _record_payload(manager.long_term.get(memory_id))


@router.patch("/long-term/{memory_id}", dependencies=[Depends(_default_limit)])
def update_long_term(
    memory_id: str,
    body: LongTermUpdateRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude_none=True, exclude={"if_version"})
    record = manager.update_long_term_memory(memory_id, payload, if_version=body.if_version)
    return _record_payload(record)


@router.delete("/long-term/{memory_id}", dependencies=[Depends(_default_limit)])
def delete_long_term(
    memory_id: str,
    request: Request,
    manager: ManagerDep,
    session_id: SessionDep,
    if_version: int | None = Query(default=None, ge=1),
    permanent: bool = Query(default=False),
) -> DeleteResponse:
    """Soft-delete by default; permanent deletion requires the admin token."""
    if permanent:
        require_admin(request, manager)
    manager.delete_long_term_memory(memory_id, permanent=permanent, if_version=if_version)
    return DeleteResponse(deleted=True, permanent=permanent, memory_id=memory_id)


# --------------------------------------------------------------------------- episodes
@router.get("/episodes", dependencies=[Depends(_default_limit)])
def list_episodes(
    manager: ManagerDep,
    session_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=80),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    include_corrected: bool = Query(default=False),
) -> dict[str, Any]:
    query = EpisodicQuery(
        session_id=session_id,
        event_type=event_type,
        since=since,
        until=until,
        limit=limit,
        include_corrected=include_corrected,
    )
    result = manager.read_episodes(query)
    return {
        "count": len(result.events),
        "events": [
            {
                "episode": view.episode.model_dump(mode="json"),
                "corrected": view.corrected,
                "corrected_by": view.corrected_by,
                "target_status": view.target_status,
                "correction_status": view.correction_status,
            }
            for view in result.events
        ],
        "corrupt_lines": result.corrupt_lines,
        "total_lines": result.total_lines,
        "quarantined": len(result.quarantined),
        "unresolved_corrections": result.unresolved_corrections,
    }


@router.post("/episodes", dependencies=[Depends(_default_limit)])
def create_episode(
    body: EpisodeCreateRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    episode = manager.append_episode(
        session_id=body.session_id or session_id,
        event_type=body.event_type,
        summary=body.summary,
        outcome=body.outcome,
        related_memory_ids=body.related_memory_ids,
        correction_of=body.correction_of,
        payload=body.payload,
    )
    return episode.model_dump(mode="json")


@router.get("/episodes/quarantine", dependencies=[Depends(_default_limit)])
def list_quarantine(manager: ManagerDep) -> dict[str, Any]:
    records = manager.pending_quarantine()
    return {
        "count": len(records),
        "records": [record.model_dump(mode="json") for record in records],
        "note": "Quarantined lines are isolated; events.jsonl itself is never modified.",
    }


@router.post(
    "/episodes/quarantine/acknowledge",
    dependencies=[Depends(_default_limit), Depends(require_admin)],
)
def acknowledge_quarantine(
    body: QuarantineAcknowledgeRequest,
    manager: ManagerDep,
    request: Request,
) -> dict[str, Any]:
    """Allow further appends for the named quarantine records (not a repair)."""
    if body.confirm is not True:
        raise NanoMemoryError(
            code=ErrorCode.CONFIRMATION_REQUIRED,
            message="Acknowledgement requires confirm=true.",
        )
    acks = manager.acknowledge_quarantine(
        body.quarantine_ids,
        reason=body.reason,
        request_id=getattr(request.state, "request_id", None),
    )
    return {
        "acknowledged": [ack.quarantine_id for ack in acks],
        "count": len(acks),
        "note": "Acknowledgement permits appends; the quarantine records are not repaired.",
    }


# --------------------------------------------------------------------------- capacity/stats
@router.get("/capacity", dependencies=[Depends(_default_limit)])
def capacity(manager: ManagerDep) -> dict[str, Any]:
    return manager.long_term.capacity()


@router.get("/statistics", dependencies=[Depends(_default_limit)])
def statistics(manager: ManagerDep) -> dict[str, Any]:
    return manager.get_memory_statistics()


@router.get("/export", dependencies=[Depends(require_admin)])
def export_memory(manager: ManagerDep) -> dict[str, Any]:
    """Full data export (admin only; may contain memory contents)."""
    return manager.export_snapshot()


__all__ = ["router"]
