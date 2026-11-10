"""Request/response schemas for the public REST API (spec §12)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nanomemory.core.memory_models import (
    Confidence,
    GoalStatus,
    MemoryStatus,
    SourceType,
    TrustLevel,
)


class WorkingMemoryProposalRequest(BaseModel):
    """PUT /api/v1/memory/working body (optimistic concurrency is mandatory)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, description="Current memory_version the client saw")
    current_goal: str | None = Field(default=None, max_length=2000)
    status: GoalStatus | None = None
    relevant_context: list[str] | None = None
    completed: list[str] | None = None
    in_progress: list[str] | None = None
    blockers: list[str] | None = None
    next_action: str | None = Field(default=None, max_length=2000)
    allow_task_removal: bool = False


class LongTermCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=3, max_length=20_000)
    category: str = Field(default="general", min_length=1, max_length=80)
    source: str = Field(default="user", min_length=1, max_length=500)
    source_type: SourceType = SourceType.USER
    confidence: Confidence = Confidence.MEDIUM
    importance: int = Field(default=3, ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=30)
    trust_level: TrustLevel | None = None


class LongTermUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=3, max_length=20_000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    tags: list[str] | None = None
    confidence: Confidence | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    status: MemoryStatus | None = None


class EpisodeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=2, max_length=80)
    summary: str = Field(default="", max_length=4000)
    outcome: str = Field(default="recorded", max_length=100)
    related_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    correction_of: str | None = Field(default=None, max_length=80)
    payload: dict[str, Any] | None = None
    session_id: str | None = None


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8000)
    max_iterations: int | None = Field(default=None, ge=1, le=20)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    allow_write_memory: bool = False


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="manual", max_length=120)


class RestoreRequest(BaseModel):
    """Restore needs an explicit confirmation equal to the backup id (§22.5)."""

    model_config = ConfigDict(extra="forbid")

    backup_id: str
    confirmation: str
    force: bool = False


class QuarantineAcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quarantine_ids: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=3, max_length=500)
    confirm: bool = Field(description="Must be true; acknowledgement allows appends only")


class CompressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["deterministic", "llm_assisted"] = "deterministic"
    apply: bool = False


class SessionCreateResponse(BaseModel):
    session_id: str
    created_at: str
    status: str
    note: str = (
        "Local single-user MVP: sessions isolate Working Memory files, not users. "
        "Send X-NanoMemory-Session-ID on subsequent requests."
    )


class DeleteResponse(BaseModel):
    deleted: bool
    permanent: bool
    memory_id: str


__all__ = [
    "AgentRunRequest",
    "BackupRequest",
    "CompressRequest",
    "DeleteResponse",
    "EpisodeCreateRequest",
    "LongTermCreateRequest",
    "LongTermUpdateRequest",
    "QuarantineAcknowledgeRequest",
    "RestoreRequest",
    "SessionCreateResponse",
    "WorkingMemoryProposalRequest",
]
