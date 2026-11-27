"""Vector memory service: the only entry point for vector reads and writes.

The service owns the lifecycle state machine, the two-phase write (accept -> embed), embedding
retries, version history, tenant/ACL enforcement and trace emission.  Routes and other
services never touch the store or the embedding provider directly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.providers.base import ProviderError
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.embeddings import EmbeddingProvider, build_embedding_provider
from nanomemory.vector.repository import VectorMemoryRepository
from nanomemory.vector.retrieval import (
    ScoredCandidate,
    content_signal,
    detect_intent,
    rank_candidates,
)
from nanomemory.vector.schemas import (
    EmbeddingResult,
    FeedbackRecord,
    MemoryFilter,
    MemoryMetadata,
    SearchHit,
    SearchResponse,
    TenantContext,
    TraceLog,
    VectorMemoryRecord,
    VectorMemoryState,
    estimate_tokens,
    new_memory_id,
    new_trace_id,
    sha256_hex,
    utc_now_iso,
)
from nanomemory.vector.store import SQLiteVectorStore
from nanomemory.vector.trace import TraceStore
from nanomemory.vector.trace_policy import TracePolicy

log = logging.getLogger("nanomemory.vector")

#: Allowed lifecycle transitions.  Anything else is a 409, never a silent state change.
TRANSITIONS: dict[VectorMemoryState, frozenset[VectorMemoryState]] = {
    VectorMemoryState.PENDING_EMBEDDING: frozenset(
        {VectorMemoryState.ACTIVE, VectorMemoryState.DELETED}
    ),
    VectorMemoryState.ACTIVE: frozenset(
        {
            VectorMemoryState.ARCHIVED,
            VectorMemoryState.DELETED,
            VectorMemoryState.ROLLED_BACK,
        }
    ),
    VectorMemoryState.ARCHIVED: frozenset(
        {
            VectorMemoryState.ACTIVE,
            VectorMemoryState.DELETED,
            VectorMemoryState.ROLLED_BACK,
        }
    ),
    VectorMemoryState.ROLLED_BACK: frozenset(
        {VectorMemoryState.ACTIVE, VectorMemoryState.ARCHIVED, VectorMemoryState.DELETED}
    ),
    VectorMemoryState.DELETED: frozenset(),
}


@dataclass(slots=True)
class NewMemory:
    """Validated write payload (scope comes from TenantContext, never from here)."""

    content: str
    metadata: MemoryMetadata
    acl_tags: tuple[str, ...] = ()
    memory_id: str | None = None
    """Stable id supplied by a migration; normal API writes let the service generate one."""


@dataclass(slots=True)
class WriteOutcome:
    record: VectorMemoryRecord
    trace_id: str
    embedding: EmbeddingResult | None = None
    idempotent_replay: bool = False


class VectorMemoryService:
    """Orchestrates embeddings, the vector store, lifecycle and traces."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: EmbeddingProvider | None = None,
        database: VectorDatabase | None = None,
    ) -> None:
        self.settings = settings
        self._provider = provider
        self._database = database
        self._store: VectorMemoryRepository | None = None
        self._traces: TraceStore | None = None
        self._ready = False
        self._lock = threading.Lock()
        self.trace_policy = TracePolicy.from_settings(settings)

    # ------------------------------------------------------------------ wiring
    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            database = self._database or VectorDatabase(self.settings.vector_database_path)
            self._store = SQLiteVectorStore(database)
            self._traces = TraceStore(database)
            self._provider = self._provider or build_embedding_provider(self.settings)
            self._ready = True

    @property
    def store(self) -> VectorMemoryRepository:
        self.ensure_ready()
        assert self._store is not None
        return self._store

    @property
    def traces(self) -> TraceStore:
        self.ensure_ready()
        assert self._traces is not None
        return self._traces

    @property
    def provider(self) -> EmbeddingProvider:
        self.ensure_ready()
        assert self._provider is not None
        return self._provider

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
        self._ready = False

    # ------------------------------------------------------------------ helpers
    @property
    def model_name(self) -> str:
        return self.provider.model_name

    def _acl(self, context: TenantContext, requested: tuple[str, ...]) -> tuple[str, ...]:
        """A caller may only share with audiences it already belongs to."""
        cleaned = tuple(dict.fromkeys(tag.strip().lower() for tag in requested if tag.strip()))
        if not set(cleaned) <= set(context.acl_tags):
            raise NanoMemoryError(
                code=ErrorCode.TENANT_SCOPE_VIOLATION,
                message="acl_tags must be a subset of the caller's own acl_tags.",
                details={"requested": list(cleaned), "granted": list(context.acl_tags)},
            )
        return cleaned

    @staticmethod
    def _assert_transition(current: VectorMemoryState, target: VectorMemoryState) -> None:
        if target is current:
            return
        if target not in TRANSITIONS.get(current, frozenset()):
            raise NanoMemoryError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=f"Cannot move a memory from {current.value} to {target.value}.",
                details={"from": current.value, "to": target.value},
            )

    def _cost(self, tokens: int) -> float:
        price = float(self.settings.embedding_cost_per_1k_tokens)
        return round(tokens / 1000.0 * price, 8) if price else 0.0

    async def _embed(self, texts: list[str]) -> EmbeddingResult:
        attempts = max(1, int(self.settings.embedding_max_retries) + 1)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = await self.provider.embed(texts)
            except ProviderError as exc:
                last = exc
                retryable = exc.retryable is not False
                if attempt < attempts and retryable:
                    await asyncio.sleep(min(0.5, 0.05 * attempt))
                    continue
                raise
            if result.dim != int(self.settings.embedding_dimension):
                raise NanoMemoryError(
                    code=ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                    message="Embedding dimension does not match NANOMEMORY_EMBEDDING_DIMENSION.",
                    details={
                        "provider": result.dim,
                        "configured": int(self.settings.embedding_dimension),
                    },
                )
            return result
        raise last  # pragma: no cover - loop either returns or raises

    def _embedding_error(self, exc: Exception, memory_id: str) -> NanoMemoryError:
        category = getattr(exc, "category", None)
        return NanoMemoryError(
            code=ErrorCode.EMBEDDING_FAILED,
            message=(
                "Memory was accepted but embedding failed; it is stored as pending_embedding "
                "and is not retrievable until retried."
            ),
            details={
                "memory_id": memory_id,
                "state": VectorMemoryState.PENDING_EMBEDDING.value,
                "reason": str(getattr(category, "value", category) or type(exc).__name__),
            },
        )

    def _record_trace(self, trace: TraceLog) -> None:
        try:
            self.traces.record(self.trace_policy.apply(trace))
        except Exception:  # noqa: BLE001 - observability must not break the request
            log.warning("trace_write_failed", extra={"event": "trace_write_failed"})

    # ------------------------------------------------------------------ write
    async def remember(
        self,
        context: TenantContext,
        payload: NewMemory,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> WriteOutcome:
        started = time.perf_counter()
        content = payload.content.strip()
        if not content:
            raise NanoMemoryError(
                code=ErrorCode.VALIDATION_FAILED, message="content must not be blank."
            )
        if idempotency_key:
            existing = self.store.idempotent_lookup(idempotency_key, context.tenant_id)
            if existing:
                record = self.store.get(existing, context.tenant_id)
                return WriteOutcome(record=record, trace_id="", idempotent_replay=True)

        record = VectorMemoryRecord(
            id=payload.memory_id or new_memory_id(),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            content=content,
            metadata=payload.metadata,
            acl_tags=self._acl(context, payload.acl_tags),
            state=VectorMemoryState.PENDING_EMBEDDING,
            embedding_model=self.model_name,
            state_reason="awaiting embedding",
        )
        self.store.insert(record, None, reason="accepted")

        trace_id = new_trace_id()
        try:
            embedding = await self._embed([content])
        except Exception as exc:  # noqa: BLE001 - classified into a typed error below
            failed = record.model_copy(
                update={
                    "state_reason": f"embedding_failed: {type(exc).__name__}"[:500],
                    "updated_at": utc_now_iso(),
                }
            )
            self.store.save(failed, None, reason="embedding_failed")
            self._record_trace(
                TraceLog(
                    trace_id=trace_id,
                    kind="write",
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    request_id=request_id,
                    query_raw=content[:4000],
                    filter_applied={"acl_tags": list(record.acl_tags)},
                    total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    input_tokens=estimate_tokens(content),
                    embedding_model=self.model_name,
                    insufficient_context=False,
                    degraded=True,
                    metadata={
                        "event": "embedding_failed",
                        "memory_id": record.id,
                        "error": type(exc).__name__,
                    },
                )
            )
            raise self._embedding_error(exc, record.id) from exc

        active = record.model_copy(
            update={
                "state": VectorMemoryState.ACTIVE,
                "state_reason": None,
                "embedding_model": embedding.model,
                "embedding_dim": embedding.dim,
                "updated_at": utc_now_iso(),
            }
        )
        vector = np.asarray(embedding.vectors[0], dtype=np.float32)
        self.store.save(active, vector, reason="embedded")
        if idempotency_key:
            self.store.idempotent_store(idempotency_key, context.tenant_id, active.id)
        self._record_trace(
            TraceLog(
                trace_id=trace_id,
                kind="write",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                request_id=request_id,
                query_raw=content[:4000],
                filter_applied={"acl_tags": list(active.acl_tags)},
                retrieved_chunk_ids=[active.id],
                total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=embedding.tokens or estimate_tokens(content),
                estimated_cost=self._cost(embedding.tokens),
                embedding_model=embedding.model,
                degraded=False,
                metadata={"event": "memory_created", "version": active.version},
            )
        )
        return WriteOutcome(record=active, trace_id=trace_id, embedding=embedding)

    async def retry_embedding(
        self, context: TenantContext, memory_id: str, *, request_id: str | None = None
    ) -> WriteOutcome:
        record = self.store.get(memory_id, context.tenant_id)
        if record.state is not VectorMemoryState.PENDING_EMBEDDING:
            raise NanoMemoryError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message="Only pending_embedding memories can be retried.",
                details={"id": memory_id, "state": record.state.value},
            )
        started = time.perf_counter()
        try:
            embedding = await self._embed([record.content])
        except Exception as exc:  # noqa: BLE001
            failed = record.model_copy(
                update={
                    "state_reason": f"embedding_failed: {type(exc).__name__}"[:500],
                    "updated_at": utc_now_iso(),
                }
            )
            self.store.save(failed, None, reason="embedding_failed")
            raise self._embedding_error(exc, memory_id) from exc

        active = record.model_copy(
            update={
                "state": VectorMemoryState.ACTIVE,
                "state_reason": None,
                "embedding_model": embedding.model,
                "embedding_dim": embedding.dim,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.save(
            active, np.asarray(embedding.vectors[0], dtype=np.float32), reason="retried"
        )
        trace_id = new_trace_id()
        self._record_trace(
            TraceLog(
                trace_id=trace_id,
                kind="write",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                request_id=request_id,
                query_raw=active.content[:4000],
                retrieved_chunk_ids=[active.id],
                total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=embedding.tokens,
                estimated_cost=self._cost(embedding.tokens),
                embedding_model=embedding.model,
                metadata={"event": "embedding_retried"},
            )
        )
        return WriteOutcome(record=active, trace_id=trace_id, embedding=embedding)

    async def update(
        self,
        context: TenantContext,
        memory_id: str,
        *,
        content: str | None = None,
        metadata: MemoryMetadata | None = None,
        acl_tags: tuple[str, ...] | None = None,
        expected_version: int | None = None,
        request_id: str | None = None,
    ) -> WriteOutcome:
        started = time.perf_counter()
        current = self.store.get(memory_id, context.tenant_id)
        if current.state is VectorMemoryState.DELETED:
            raise NanoMemoryError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message="Deleted memories cannot be updated; create a new memory instead.",
                details={"id": memory_id},
            )
        if expected_version is not None and current.version != expected_version:
            raise NanoMemoryError(
                code=ErrorCode.VERSION_CONFLICT,
                message=f"Vector memory version conflict for {memory_id}.",
                details={
                    "id": memory_id,
                    "expected_version": expected_version,
                    "current_version": current.version,
                },
            )
        new_content = (content or current.content).strip()
        if not new_content:
            raise NanoMemoryError(
                code=ErrorCode.VALIDATION_FAILED, message="content must not be blank."
            )
        new_metadata = metadata or current.metadata
        new_acl = current.acl_tags if acl_tags is None else self._acl(context, acl_tags)

        embedding: EmbeddingResult | None = None
        vector: np.ndarray | None = None
        if new_content != current.content or current.state is VectorMemoryState.PENDING_EMBEDDING:
            try:
                embedding = await self._embed([new_content])
            except Exception as exc:  # noqa: BLE001 - keep the previous version intact
                raise self._embedding_error(exc, memory_id) from exc
            vector = np.asarray(embedding.vectors[0], dtype=np.float32)

        target_state = (
            VectorMemoryState.ACTIVE
            if current.state is VectorMemoryState.PENDING_EMBEDDING
            else current.state
        )
        updated = current.model_copy(
            update={
                "content": new_content,
                "metadata": new_metadata,
                "acl_tags": new_acl,
                "state": target_state,
                "state_reason": None,
                "version": current.version + 1,
                "content_sha256": sha256_hex(new_content),
                "updated_at": utc_now_iso(),
                "embedding_model": embedding.model if embedding else current.embedding_model,
                "embedding_dim": embedding.dim if embedding else current.embedding_dim,
            }
        )
        self.store.save(updated, vector, reason="updated")
        trace_id = new_trace_id()
        self._record_trace(
            TraceLog(
                trace_id=trace_id,
                kind="update",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                request_id=request_id,
                query_raw=new_content[:4000],
                retrieved_chunk_ids=[memory_id],
                total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=embedding.tokens if embedding else 0,
                estimated_cost=self._cost(embedding.tokens) if embedding else 0.0,
                embedding_model=updated.embedding_model,
                metadata={"event": "memory_updated", "version": updated.version},
            )
        )
        return WriteOutcome(record=updated, trace_id=trace_id, embedding=embedding)

    # ------------------------------------------------------------------ lifecycle
    def _transition_record(
        self,
        context: TenantContext,
        record: VectorMemoryRecord,
        target: VectorMemoryState,
        *,
        reason: str,
        expected_version: int | None = None,
    ) -> VectorMemoryRecord:
        if expected_version is not None and record.version != expected_version:
            raise NanoMemoryError(
                code=ErrorCode.VERSION_CONFLICT,
                message=f"Vector memory version conflict for {record.id}.",
                details={
                    "id": record.id,
                    "expected_version": expected_version,
                    "current_version": record.version,
                },
            )
        self._assert_transition(record.state, target)
        now = utc_now_iso()
        updated = record.model_copy(
            update={
                "state": target,
                "state_reason": reason[:500],
                "version": record.version + 1,
                "updated_at": now,
                "deleted_at": now if target is VectorMemoryState.DELETED else None,
            }
        )
        self.store.save(updated, None, reason=reason)
        return updated

    def archive(
        self, context: TenantContext, memory_id: str, *, expected_version: int | None = None
    ) -> VectorMemoryRecord:
        record = self.store.get(memory_id, context.tenant_id)
        return self._transition_record(
            context,
            record,
            VectorMemoryState.ARCHIVED,
            reason="archived by operator",
            expected_version=expected_version,
        )

    def delete(
        self, context: TenantContext, memory_id: str, *, expected_version: int | None = None
    ) -> VectorMemoryRecord:
        record = self.store.get(memory_id, context.tenant_id)
        return self._transition_record(
            context,
            record,
            VectorMemoryState.DELETED,
            reason="soft deleted by operator",
            expected_version=expected_version,
        )

    def reactivate(self, context: TenantContext, memory_id: str) -> VectorMemoryRecord:
        record = self.store.get(memory_id, context.tenant_id)
        if record.embedding_dim <= 0:
            raise NanoMemoryError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message="Memory has no embedding yet; retry embedding instead of reactivating.",
                details={"id": memory_id, "state": record.state.value},
            )
        return self._transition_record(
            context, record, VectorMemoryState.ACTIVE, reason="reactivated by operator"
        )

    async def rollback(
        self,
        context: TenantContext,
        memory_id: str,
        *,
        target_version: int,
        request_id: str | None = None,
    ) -> WriteOutcome:
        """Restore a historical version as a NEW version in the ROLLED_BACK state.

        Rollback is deliberately not auto-active: a restored memory has to be reviewed and
        reactivated before retrieval can serve it again.
        """
        started = time.perf_counter()
        current = self.store.get(memory_id, context.tenant_id)
        self._assert_transition(current.state, VectorMemoryState.ROLLED_BACK)
        snapshot = self.store.version_snapshot(current.id, context.tenant_id, target_version)
        if snapshot is None:
            raise NanoMemoryError(
                code=ErrorCode.MEMORY_NOT_FOUND,
                message="The requested memory version does not exist.",
                details={"id": memory_id, "version": target_version},
            )
        vector = snapshot["vector"]
        if vector is None:
            embedding = await self._embed([snapshot["content"]])
            vector = np.asarray(embedding.vectors[0], dtype=np.float32)
            model, dim = embedding.model, embedding.dim
        else:
            model, dim = snapshot["embedding_model"], int(snapshot["embedding_dim"])
        metadata = MemoryMetadata.model_validate(snapshot["metadata"])
        restored = current.model_copy(
            update={
                "content": snapshot["content"],
                "metadata": metadata,
                "acl_tags": tuple(snapshot["acl_tags"]),
                "state": VectorMemoryState.ROLLED_BACK,
                "state_reason": f"rolled back to version {target_version}",
                "version": current.version + 1,
                "content_sha256": sha256_hex(snapshot["content"]),
                "updated_at": utc_now_iso(),
                "rolled_back_from": target_version,
                "embedding_model": model,
                "embedding_dim": dim,
            }
        )
        self.store.save(restored, vector, reason=f"rolled_back_to_v{target_version}")
        trace_id = new_trace_id()
        self._record_trace(
            TraceLog(
                trace_id=trace_id,
                kind="rollback",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                request_id=request_id,
                query_raw=restored.content[:4000],
                retrieved_chunk_ids=[memory_id],
                total_latency_ms=round((time.perf_counter() - started) * 1000, 2),
                embedding_model=model,
                metadata={"event": "rolled_back", "target_version": target_version},
            )
        )
        return WriteOutcome(record=restored, trace_id=trace_id)

    # ------------------------------------------------------------------ read
    def get(self, context: TenantContext, memory_id: str) -> VectorMemoryRecord:
        return self.store.get(memory_id, context.tenant_id)

    def list(
        self,
        context: TenantContext,
        *,
        limit: int = 50,
        offset: int = 0,
        states: tuple[VectorMemoryState, ...] | None = None,
        include_archived: bool = False,
    ) -> list[VectorMemoryRecord]:
        if states is not None:
            filter = MemoryFilter.for_context(context, states=states)
        elif include_archived:
            filter = MemoryFilter.for_context(
                context,
                states=(
                    VectorMemoryState.ACTIVE,
                    VectorMemoryState.ARCHIVED,
                    VectorMemoryState.PENDING_EMBEDDING,
                    VectorMemoryState.ROLLED_BACK,
                ),
            )
        else:
            filter = MemoryFilter.for_context(context)
        return self.store.list(filter, limit=limit, offset=offset)

    def versions(self, context: TenantContext, memory_id: str) -> list[dict[str, Any]]:
        return self.store.versions(memory_id, context.tenant_id)

    async def search(
        self,
        context: TenantContext,
        query: str,
        *,
        limit: int | None = None,
        min_score: float | None = None,
        categories: tuple[str, ...] = (),
        memory_types: tuple[Any, ...] = (),
        tags: tuple[str, ...] = (),
        request_id: str | None = None,
        store_context: bool = True,
        allow_weak: bool = False,
    ) -> SearchResponse:
        started = time.perf_counter()
        text = query.strip()
        if not text:
            raise NanoMemoryError(
                code=ErrorCode.VALIDATION_FAILED, message="query must not be blank."
            )
        effective_limit = int(limit or self.settings.vector_search_limit)
        floor = float(self.settings.vector_content_floor)
        result_floor = float(
            self.settings.vector_min_result_score if min_score is None else min_score
        )
        filter = MemoryFilter.for_context(
            context,
            categories=tuple(categories),
            memory_types=tuple(memory_types),
            tags=tuple(tags),
        )
        trace_id = new_trace_id()
        degraded = False
        fallback_reason: str | None = None
        query_embedding: EmbeddingResult | None = None
        query_vector: np.ndarray | None = None
        embedding_ms = 0.0

        embed_started = time.perf_counter()
        try:
            query_embedding = await self._embed([text])
            query_vector = np.asarray(query_embedding.vectors[0], dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 - dense failure degrades to lexical retrieval
            degraded = True
            fallback_reason = f"embedding_unavailable: {type(exc).__name__}"
        embedding_ms = round((time.perf_counter() - embed_started) * 1000, 2)

        retrieve_started = time.perf_counter()
        scanned = 0
        scored: list[ScoredCandidate] = []
        if query_vector is not None:
            records, dense_scores, scanned = self.store.score_all(
                query_vector,
                filter,
                embedding_model=self.model_name,
                limit=int(self.settings.vector_candidate_limit),
            )
            if scanned == 0 and self.store.count(
                MemoryFilter.for_context(
                    context,
                    states=(VectorMemoryState.ACTIVE, VectorMemoryState.ARCHIVED),
                )
            ):
                degraded = True
                fallback_reason = (
                    "embedding_model_changed: stored vectors were written by another model "
                    "and must be re-embedded before dense retrieval can see them"
                )
        else:
            records = self.store.list(filter, limit=int(self.settings.vector_candidate_limit))
            dense_scores = [0.0] * len(records)
            scanned = len(records)
        retrieve_ms = round((time.perf_counter() - retrieve_started) * 1000, 2)

        scored = rank_candidates(records, dense_scores, text)
        signal = content_signal(scored)
        insufficient = signal < floor or not scored
        hits = [
            SearchHit(
                id=candidate.record.id,
                content=candidate.record.content,
                score=candidate.score,
                metadata=candidate.record.metadata,
                state=candidate.record.state,
                version=candidate.record.version,
                signals=candidate.signals,
                reason=candidate.reason,
            )
            for candidate in scored
            if candidate.score >= result_floor
        ][:effective_limit]
        # Refusal is a contract, not a hint: insufficient_context implies no items.  A caller
        # that needs the weak candidates for debugging must ask explicitly (allow_weak).
        if insufficient and not allow_weak:
            hits = []
        if insufficient and not fallback_reason:
            fallback_reason = (
                f"best content signal {signal:.3f} is below the floor {floor:.2f}; "
                "the memory corpus does not contain a confident answer"
            )
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        context_block = None
        if store_context and hits:
            context_block = "\n\n".join(f"[{hit.id}] {hit.content}" for hit in hits)[
                : int(self.settings.vector_trace_context_chars)
            ]

        self._record_trace(
            TraceLog(
                trace_id=trace_id,
                kind="search",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                request_id=request_id,
                query_raw=text,
                filter_applied=filter.describe(),
                retrieved_chunk_ids=[hit.id for hit in hits],
                similarity_scores=[hit.score for hit in hits],
                final_prompt_context=context_block,
                total_latency_ms=total_ms,
                input_tokens=query_embedding.tokens if query_embedding else estimate_tokens(text),
                estimated_cost=self._cost(query_embedding.tokens) if query_embedding else 0.0,
                embedding_model=self.model_name,
                insufficient_context=insufficient,
                degraded=degraded,
                metadata={
                    "intent": detect_intent(text).value,
                    "candidates": scanned,
                    "best_content_signal": round(signal, 4),
                    "embedding_ms": embedding_ms,
                    "retrieve_ms": retrieve_ms,
                },
            )
        )
        return SearchResponse(
            query=text,
            trace_id=trace_id,
            items=hits,
            candidate_count=scanned,
            insufficient_context=insufficient,
            fallback_reason=fallback_reason,
            degraded=degraded,
            embedding_model=self.model_name,
            content_signal=round(signal, 4),
            timings_ms={
                "embedding": embedding_ms,
                "retrieval": retrieve_ms,
                "total": total_ms,
            },
        )

    # ------------------------------------------------------------------ feedback
    def feedback(
        self,
        context: TenantContext,
        *,
        trace_id: str,
        verdict: str,
        reason: str | None = None,
        comment: str | None = None,
    ) -> FeedbackRecord:
        record = FeedbackRecord(
            trace_id=trace_id,
            verdict=verdict,  # type: ignore[arg-type]
            reason=reason,
            comment=comment,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        return self.traces.add_feedback(record)

    def trace(self, context: TenantContext, trace_id: str) -> TraceLog:
        return self.traces.get(trace_id, tenant_id=context.tenant_id)

    def export_badcases(self, context: TenantContext, path: Path) -> int:
        return self.traces.export_badcases(context.tenant_id, path)

    def prune_traces(self, context: TenantContext, days: int | None = None) -> dict[str, int]:
        """Delete traces older than the retention window for this tenant only."""
        effective = int(days or self.trace_policy.retention_days)
        return self.traces.prune(context.tenant_id, effective)

    # ------------------------------------------------------------------ stats
    def stats(self, context: TenantContext) -> dict[str, Any]:
        return {
            "tenant_id": context.tenant_id,
            "storage": self.store.stats(context.tenant_id),
            "traces": self.traces.stats(context.tenant_id),
            "embedding": {
                **self.provider.describe(),
                "configured_dim": int(self.settings.embedding_dimension),
                "cache": getattr(self.provider, "cache_stats", lambda: {})(),
            },
            "trace_policy": {
                "content_mode": self.trace_policy.mode,
                "retention_days": self.trace_policy.retention_days,
            },
            "retrieval": {
                "content_floor": self.settings.vector_content_floor,
                "min_result_score": self.settings.vector_min_result_score,
                "candidate_limit": self.settings.vector_candidate_limit,
            },
            "context": {
                "signed": context.signed,
                "signing_configured": self.settings.context_signing_key is not None,
            },
            "as_of": datetime.now(UTC).isoformat(),
        }


__all__ = [
    "TRANSITIONS",
    "NewMemory",
    "VectorMemoryService",
    "WriteOutcome",
]
