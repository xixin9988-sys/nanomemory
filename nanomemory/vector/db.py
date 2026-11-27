"""SQLite persistence for vector memory.

One database file holds memories, their version history, ACL/tag projections, traces and
feedback.  SQLite is chosen deliberately for the single-node product: it gives transactional
versioning and a real SQL WHERE clause for pre-filtering without an external service.  The
store interface keeps a pgvector adapter possible without touching callers.

Schema changes go through SCHEMA_VERSION; the service refuses to start on a database written
by a newer build instead of corrupting it.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from nanomemory.core.errors import ErrorCode, NanoMemoryError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_memories (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL,
    category TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    valid_until TEXT,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    embedding BLOB,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    rolled_back_from INTEGER,
    state_reason TEXT,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vm_tenant_state ON vector_memories (tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_vm_tenant_user_state
    ON vector_memories (tenant_id, user_id, state);
CREATE INDEX IF NOT EXISTS idx_vm_model ON vector_memories (embedding_model);
CREATE INDEX IF NOT EXISTS idx_vm_category ON vector_memories (tenant_id, category);

CREATE TABLE IF NOT EXISTS memory_acl (
    memory_id TEXT NOT NULL REFERENCES vector_memories (id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_acl_tag ON memory_acl (tag);

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT NOT NULL REFERENCES vector_memories (id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_memory_tag ON memory_tags (tag);

CREATE TABLE IF NOT EXISTS memory_versions (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL,
    state TEXT NOT NULL,
    embedding BLOB,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    acl_tags TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (memory_id, version)
);

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request_id TEXT,
    query_raw TEXT,
    filter_applied TEXT NOT NULL,
    retrieved_chunk_ids TEXT NOT NULL,
    similarity_scores TEXT NOT NULL,
    final_prompt_context TEXT,
    model_response TEXT,
    ttft_ms REAL,
    total_latency_ms REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost REAL NOT NULL,
    embedding_model TEXT,
    insufficient_context INTEGER NOT NULL,
    degraded INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_tenant_created ON traces (tenant_id, created_at);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT,
    comment TEXT,
    tenant_id TEXT,
    user_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_trace ON feedback (trace_id);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class VectorDatabase:
    """Owns the SQLite connection, schema version and write serialisation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # ------------------------------------------------------------------ lifecycle
    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            found = int(row["value"])
            if found > SCHEMA_VERSION:
                raise NanoMemoryError(
                    code=ErrorCode.SCHEMA_UNSUPPORTED,
                    message="Vector store schema is newer than this build supports.",
                    details={"found": found, "supported": SCHEMA_VERSION},
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ access
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers and commit atomically (rollback on any exception)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn


__all__ = ["SCHEMA_VERSION", "VectorDatabase"]
