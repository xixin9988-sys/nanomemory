"""Strongly typed contract for vector memory (the single source of truth).

Every value that crosses a module boundary or is persisted is one of these models, so an
invalid record cannot reach SQLite and an API response cannot drift from the stored schema
(the v2 answer to "never rely on prompt-shaped JSON").

Scope rule: TenantContext is the only carrier of tenant_id/user_id.  Request bodies never
contain a scope field, which is what makes pre-filtering unforgeable.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nanomemory.core.memory_models import (
    Confidence,
    MemoryType,
    Provenance,
    SourceType,
    TrustLevel,
)

VECTOR_SCHEMA_VERSION = 1

_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,80}$"
_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,39}$")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_memory_id() -> str:
    return f"vec-{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    return f"tr-{uuid.uuid4().hex[:20]}"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent token estimate (4 chars/token, CJK counts double).

    Trace logs record both this estimate and the provider's reported usage when available;
    the field name (input_tokens) is the product's accounting unit, not billing truth.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    rest = len(text) - cjk
    return max(1, int(round(cjk / 1.7 + rest / 4.0)))


class VectorMemoryState(StrEnum):
    """Lifecycle states.  Only ACTIVE rows are visible to retrieval."""

    PENDING_EMBEDDING = "pending_embedding"
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"
    ROLLED_BACK = "rolled_back"


class TenantContext(BaseModel):
    """Verified caller scope.

    tenant_id is mandatory; user_id names the owner of private rows; acl_tags grant access to
    rows that opted into sharing (record ACL is a fixed tag set, not a per-call policy -- see
    docs/internal/VECTOR_ARCHITECTURE.md).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=80, pattern=_ID_PATTERN)
    user_id: str = Field(min_length=1, max_length=80, pattern=_ID_PATTERN)
    roles: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    acl_tags: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    request_id: str | None = Field(default=None, max_length=80)
    signed: bool = False
    """True when the context arrived in a verifiable envelope from the gateway."""

    @field_validator("roles", "acl_tags")
    @classmethod
    def _clean_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for item in value:
            normalised = item.strip().lower()
            if not normalised:
                continue
            if not _TAG_PATTERN.match(normalised):
                raise ValueError("acl tags must be lowercase slugs of at most 40 characters")
            if normalised not in cleaned:
                cleaned.append(normalised)
        return tuple(cleaned)


class MemoryMetadata(BaseModel):
    """Taxonomy carried alongside the vector (never inside the embedding prompt)."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(default="general", min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=30)
    memory_type: MemoryType = MemoryType.FACT
    importance: int = Field(default=3, ge=1, le=5)
    source: str = Field(default="unknown", min_length=1, max_length=500)
    source_type: SourceType = SourceType.UNKNOWN
    confidence: Confidence = Confidence.MEDIUM
    trust_level: TrustLevel = TrustLevel.UNKNOWN
    valid_from: str | None = Field(default=None, max_length=40)
    valid_until: str | None = Field(default=None, max_length=40)
    provenance: Provenance = Field(default_factory=Provenance)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def _tag_shape(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            normalised = tag.strip().lower()
            if not normalised:
                continue
            if not _TAG_PATTERN.match(normalised):
                raise ValueError(f"invalid tag: {normalised!r}")
            if normalised not in cleaned:
                cleaned.append(normalised)
        return cleaned

    @field_validator("attributes")
    @classmethod
    def _bounded_attributes(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(value) > 30:
            raise ValueError("at most 30 attributes are allowed")
        for key, item in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", key):
                raise ValueError(f"invalid attribute key: {key!r}")
            if isinstance(item, str) and len(item) > 500:
                raise ValueError("attribute values must be at most 500 characters")
        return value


class VectorMemoryRecord(BaseModel):
    """One stored memory row (the embedding itself is never part of this model)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=8, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=80)
    user_id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=20_000)
    metadata: MemoryMetadata = Field(default_factory=MemoryMetadata)
    state: VectorMemoryState = VectorMemoryState.PENDING_EMBEDDING
    version: int = Field(default=1, ge=1)
    embedding_model: str = Field(default="unknown", max_length=120)
    embedding_dim: int = Field(default=0, ge=0, le=8192)
    content_sha256: str = Field(default="", max_length=64)
    acl_tags: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    deleted_at: str | None = Field(default=None, max_length=40)
    rolled_back_from: int | None = Field(default=None, ge=1)
    state_reason: str | None = Field(default=None, max_length=500)
    schema_version: int = VECTOR_SCHEMA_VERSION

    @model_validator(mode="after")
    def _consistent(self) -> VectorMemoryRecord:
        expected = sha256_hex(self.content)
        if not self.content_sha256:
            object.__setattr__(self, "content_sha256", expected)
        elif self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match content")
        if self.state is VectorMemoryState.DELETED and not self.deleted_at:
            object.__setattr__(self, "deleted_at", self.updated_at)
        if self.state is not VectorMemoryState.DELETED and self.deleted_at:
            raise ValueError("deleted_at is only valid for deleted records")
        return self

    def to_public(self) -> dict[str, Any]:
        """API projection: owner scope and raw vectors are not echoed."""
        return {
            "id": self.id,
            "content": self.content,
            "metadata": self.metadata.model_dump(mode="json"),
            "state": self.state.value,
            "version": self.version,
            "embedding": {"model": self.embedding_model, "dim": self.embedding_dim},
            "content_sha256": self.content_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_reason": self.state_reason,
        }


class MemoryFilter(BaseModel):
    """Pre-filter applied inside the SQL query, before any similarity math."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=80)
    user_id: str | None = Field(default=None, max_length=80)
    acl_tags: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    states: tuple[VectorMemoryState, ...] = (VectorMemoryState.ACTIVE,)
    categories: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    memory_types: tuple[MemoryType, ...] = Field(default_factory=tuple, max_length=10)
    tags: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    include_expired: bool = False

    @classmethod
    def for_context(cls, context: TenantContext, **overrides: Any) -> MemoryFilter:
        data: dict[str, Any] = {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "acl_tags": context.acl_tags,
        }
        data.update(overrides)
        return cls(**data)

    def describe(self) -> dict[str, Any]:
        """Trace-safe summary; the filter never contains secrets by construction."""
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "acl_tags": list(self.acl_tags),
            "states": [state.value for state in self.states],
            "categories": list(self.categories),
            "memory_types": [item.value for item in self.memory_types],
            "include_expired": self.include_expired,
        }


class SearchQuery(BaseModel):
    """Internal (already scoped) search request."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    filter: MemoryFilter
    limit: int = Field(default=8, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    """One retrieved memory with per-signal scores (explainability is a product feature)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    content: str
    score: float = Field(ge=0.0, le=1.0)
    metadata: MemoryMetadata
    state: VectorMemoryState
    version: int
    signals: dict[str, float] = Field(default_factory=dict)
    reason: str = ""


class SearchResponse(BaseModel):
    """Retrieval result plus the observability envelope."""

    model_config = ConfigDict(extra="forbid")

    query: str
    trace_id: str
    items: list[SearchHit] = Field(default_factory=list)
    candidate_count: int = 0
    insufficient_context: bool = False
    fallback_reason: str | None = None
    degraded: bool = False
    embedding_model: str = "unknown"
    content_signal: float = 0.0
    timings_ms: dict[str, float] = Field(default_factory=dict)


class TraceLog(BaseModel):
    """One structured call record (standard #4: every retrieval is reconstructible)."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    kind: Literal["search", "write", "update", "rollback", "delete", "feedback"]
    tenant_id: str
    user_id: str
    request_id: str | None = None
    query_raw: str | None = None
    filter_applied: dict[str, Any] = Field(default_factory=dict)
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    similarity_scores: list[float] = Field(default_factory=list)
    final_prompt_context: str | None = None
    model_response: str | None = None
    ttft_ms: float | None = None
    total_latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    embedding_model: str | None = None
    insufficient_context: bool = False
    degraded: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class FeedbackRecord(BaseModel):
    """Thumbs up/down linked to a trace; the raw material of the eval loop."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(default_factory=lambda: f"fb-{uuid.uuid4().hex[:16]}")
    trace_id: str = Field(min_length=4, max_length=80)
    verdict: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=80)
    comment: str | None = Field(default=None, max_length=1000)
    tenant_id: str | None = Field(default=None, max_length=80)
    user_id: str | None = Field(default=None, max_length=80)
    created_at: str = Field(default_factory=utc_now_iso)


class EmbeddingResult(BaseModel):
    """Provider output plus the accounting metadata the trace needs."""

    model_config = ConfigDict(extra="forbid")

    vectors: list[list[float]]
    model: str
    dim: int
    tokens: int = 0
    provider: str = "unknown"


__all__ = [
    "VECTOR_SCHEMA_VERSION",
    "EmbeddingResult",
    "FeedbackRecord",
    "MemoryFilter",
    "MemoryMetadata",
    "SearchHit",
    "SearchQuery",
    "SearchResponse",
    "TenantContext",
    "TraceLog",
    "VectorMemoryRecord",
    "VectorMemoryState",
    "estimate_tokens",
    "new_memory_id",
    "new_trace_id",
    "sha256_hex",
    "utc_now_iso",
]
