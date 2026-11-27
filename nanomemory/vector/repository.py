"""Vector memory repository contract (ADR-001).

The service depends on this Protocol, never on SQLite directly.  SQLite is the first adapter;
a pgvector adapter must satisfy the same contract tests before it can be selected.  The
contract is deliberately narrow: everything the service needs, nothing that leaks SQL.

Invariant every adapter must preserve:

* every read/write is scoped by tenant_id, and the scope is applied *inside* the storage
  query before any similarity computation;
* versions are append-only and a version number is never reused;
* pending rows are never returned as retrieval candidates.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np

from nanomemory.vector.schemas import MemoryFilter, VectorMemoryRecord


@runtime_checkable
class VectorMemoryRepository(Protocol):
    """Storage contract used by VectorMemoryService."""

    def insert(
        self, record: VectorMemoryRecord, vector: np.ndarray | None, *, reason: str = "created"
    ) -> None: ...

    def save(
        self, record: VectorMemoryRecord, vector: np.ndarray | None, *, reason: str
    ) -> None: ...

    def get(self, memory_id: str, tenant_id: str) -> VectorMemoryRecord: ...

    def list(
        self, filter: MemoryFilter, *, limit: int = 50, offset: int = 0
    ) -> list[VectorMemoryRecord]: ...

    def count(self, filter: MemoryFilter) -> int: ...

    def score_all(
        self,
        query_vector: np.ndarray,
        filter: MemoryFilter,
        *,
        embedding_model: str | None = None,
        limit: int = 5000,
    ) -> tuple[list[VectorMemoryRecord], list[float], int]: ...

    def versions(self, memory_id: str, tenant_id: str) -> list[dict[str, Any]]: ...

    def version_snapshot(
        self, memory_id: str, tenant_id: str, version: int
    ) -> dict[str, Any] | None: ...

    def idempotent_lookup(self, key: str, tenant_id: str) -> str | None: ...

    def idempotent_store(self, key: str, tenant_id: str, memory_id: str) -> None: ...

    def stats(self, tenant_id: str) -> dict[str, Any]: ...


__all__ = ["VectorMemoryRepository"]
