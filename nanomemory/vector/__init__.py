"""NanoMemory vector memory (v2): embeddings, vector store, hybrid retrieval, traces.

This package is the production path added by the v2 upgrade.  The v1 JSON/markdown stores
remain supported for Working Memory and the legacy long-term API; long-term recall moves to
this package's VectorMemoryService.

Layering (each layer fails independently, no layer imports a layer above it):

    api  ->  service  ->  retrieval  ->  store  ->  db/sqlite
                |              |
                +--> embeddings +--> trace

Everything tenant-scoped takes a TenantContext; the context is produced from a signed header
envelope (see nanomemory.vector.context) and is never accepted from a request body or query
string.
"""

from nanomemory.vector.schemas import (
    FeedbackRecord,
    MemoryFilter,
    MemoryMetadata,
    SearchHit,
    SearchResponse,
    TenantContext,
    TraceLog,
    VectorMemoryRecord,
    VectorMemoryState,
)

__all__ = [
    "FeedbackRecord",
    "MemoryFilter",
    "MemoryMetadata",
    "SearchHit",
    "SearchResponse",
    "TenantContext",
    "TraceLog",
    "VectorMemoryRecord",
    "VectorMemoryState",
]
