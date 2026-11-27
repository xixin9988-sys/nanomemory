"""SQLite vector store: the only place where tenant scoping meets similarity math.

Security invariant (never relaxed): the SQL WHERE clause is built from MemoryFilter *before*
any embedding is compared.  The store never loads the tenant's whole corpus "and filters in
Python", so an implementation slip cannot turn into a cross-tenant leak -- the rows never
leave SQLite.

The store is intentionally synchronous.  SQLite in WAL mode handles the write volume of a
memory service, and the callers that need concurrency run it in a worker thread while the
embedding round-trip (the slow part) stays async.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.schemas import (
    MemoryFilter,
    MemoryMetadata,
    VectorMemoryRecord,
    VectorMemoryState,
    utc_now_iso,
)


def filter_sql(filter: MemoryFilter, *, now: str | None = None) -> tuple[str, list[Any]]:
    """Build the pre-filter WHERE clause.  Exposed for tests and for query plans."""
    clauses: list[str] = ["m.tenant_id = ?"]
    params: list[Any] = [filter.tenant_id]

    states = [state.value for state in filter.states]
    if not states:
        clauses.append("0 = 1")
    else:
        clauses.append("m.state IN (" + ",".join("?" * len(states)) + ")")
        params.extend(states)

    if filter.user_id:
        if filter.acl_tags:
            tags = [tag for tag in filter.acl_tags]
            placeholders = ",".join("?" * len(tags))
            clauses.append(
                "(m.user_id = ? OR EXISTS (SELECT 1 FROM memory_acl a "
                f"WHERE a.memory_id = m.id AND a.tag IN ({placeholders})))"
            )
            params.append(filter.user_id)
            params.extend(tags)
        else:
            clauses.append("m.user_id = ?")
            params.append(filter.user_id)

    if filter.categories:
        values = [category for category in filter.categories]
        clauses.append("m.category IN (" + ",".join("?" * len(values)) + ")")
        params.extend(values)

    if filter.memory_types:
        values = [item.value for item in filter.memory_types]
        clauses.append("m.memory_type IN (" + ",".join("?" * len(values)) + ")")
        params.extend(values)

    # Metadata tags are AND-ed: a caller asking for two tags wants both, not either.
    for tag in filter.tags:
        clauses.append(
            "EXISTS (SELECT 1 FROM memory_tags t WHERE t.memory_id = m.id AND t.tag = ?)"
        )
        params.append(tag)

    if not filter.include_expired:
        clauses.append("(m.valid_until IS NULL OR m.valid_until = '' OR m.valid_until > ?)")
        params.append(now or utc_now_iso())

    return " AND ".join(clauses), params


@dataclass(frozen=True)
class VectorRow:
    """A record plus its stored vector (internal only; never serialised to clients)."""

    record: VectorMemoryRecord
    vector: np.ndarray


@dataclass(frozen=True)
class VectorHits:
    """Ranked hits plus the number of rows the pre-filter actually admitted."""

    hits: list[tuple[VectorMemoryRecord, float]]
    scanned: int


class SQLiteVectorStore:
    backend = "sqlite"

    def __init__(self, database: VectorDatabase) -> None:
        self.db = database

    # ------------------------------------------------------------------ serialisation
    @staticmethod
    def _to_record(row: sqlite3.Row) -> VectorMemoryRecord:
        metadata = MemoryMetadata.model_validate(json.loads(row["metadata"]))
        return VectorMemoryRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            content=row["content"],
            metadata=metadata,
            state=VectorMemoryState(row["state"]),
            version=int(row["version"]),
            embedding_model=row["embedding_model"],
            embedding_dim=int(row["embedding_dim"]),
            content_sha256=row["content_sha256"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
            rolled_back_from=row["rolled_back_from"],
            state_reason=row["state_reason"],
            schema_version=int(row["schema_version"]),
        )

    @staticmethod
    def _blob(vector: np.ndarray) -> bytes:
        return np.asarray(vector, dtype=np.float32).tobytes()

    @staticmethod
    def _vector(blob: bytes | None, dim: int) -> np.ndarray | None:
        if not blob:
            return None
        array = np.frombuffer(blob, dtype=np.float32)
        if dim and array.shape[0] != dim:
            return None
        return array

    # ------------------------------------------------------------------ writes
    def insert(
        self, record: VectorMemoryRecord, vector: np.ndarray | None, *, reason: str = "created"
    ) -> None:
        with self.db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM vector_memories WHERE id = ?", (record.id,)
            ).fetchone()
            if exists:
                raise NanoMemoryError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="A vector memory with this id already exists; use patch instead.",
                    details={"id": record.id},
                )
            self._write(conn, record, vector)
            self._write_projections(conn, record)
            self._write_version(conn, record, vector, reason)

    def save(self, record: VectorMemoryRecord, vector: np.ndarray | None, *, reason: str) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE vector_memories SET user_id = ?, content = ?, metadata = ?, "
                "category = ?, memory_type = ?, valid_until = ?, state = ?, version = ?, "
                "embedding = COALESCE(?, embedding), embedding_model = ?, embedding_dim = ?, "
                "content_sha256 = ?, updated_at = ?, deleted_at = ?, rolled_back_from = ?, "
                "state_reason = ? WHERE id = ? AND tenant_id = ?",
                (
                    record.user_id,
                    record.content,
                    json.dumps(record.metadata.model_dump(mode="json"), ensure_ascii=False),
                    record.metadata.category,
                    record.metadata.memory_type.value,
                    record.metadata.valid_until,
                    record.state.value,
                    record.version,
                    self._blob(vector) if vector is not None else None,
                    record.embedding_model,
                    record.embedding_dim,
                    record.content_sha256,
                    record.updated_at,
                    record.deleted_at,
                    record.rolled_back_from,
                    record.state_reason,
                    record.id,
                    record.tenant_id,
                ),
            )
            if cursor.rowcount == 0:
                raise NanoMemoryError(
                    code=ErrorCode.MEMORY_NOT_FOUND,
                    message="Vector memory not found.",
                    details={"id": record.id},
                )
            self._write_projections(conn, record)
            self._write_version(conn, record, vector, reason)

    def _write(
        self, conn: sqlite3.Connection, record: VectorMemoryRecord, vector: np.ndarray | None
    ) -> None:
        conn.execute(
            "INSERT INTO vector_memories (id, tenant_id, user_id, content, metadata, "
            "category, memory_type, valid_until, state, version, embedding, embedding_model, "
            "embedding_dim, content_sha256, created_at, updated_at, deleted_at, "
            "rolled_back_from, state_reason, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.tenant_id,
                record.user_id,
                record.content,
                json.dumps(record.metadata.model_dump(mode="json"), ensure_ascii=False),
                record.metadata.category,
                record.metadata.memory_type.value,
                record.metadata.valid_until,
                record.state.value,
                record.version,
                self._blob(vector) if vector is not None else None,
                record.embedding_model,
                record.embedding_dim,
                record.content_sha256,
                record.created_at,
                record.updated_at,
                record.deleted_at,
                record.rolled_back_from,
                record.state_reason,
                record.schema_version,
            ),
        )

    @staticmethod
    def _write_projections(conn: sqlite3.Connection, record: VectorMemoryRecord) -> None:
        conn.execute("DELETE FROM memory_acl WHERE memory_id = ?", (record.id,))
        conn.executemany(
            "INSERT OR IGNORE INTO memory_acl (memory_id, tag) VALUES (?, ?)",
            [(record.id, tag) for tag in record.acl_tags],
        )
        conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (record.id,))
        conn.executemany(
            "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
            [(record.id, tag) for tag in record.metadata.tags],
        )

    def _write_version(
        self,
        conn: sqlite3.Connection,
        record: VectorMemoryRecord,
        vector: np.ndarray | None,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO memory_versions (memory_id, version, content, metadata, "
            "state, embedding, embedding_model, embedding_dim, content_sha256, acl_tags, "
            "created_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.version,
                record.content,
                json.dumps(record.metadata.model_dump(mode="json"), ensure_ascii=False),
                record.state.value,
                self._blob(vector) if vector is not None else None,
                record.embedding_model,
                record.embedding_dim,
                record.content_sha256,
                json.dumps(list(record.acl_tags)),
                record.updated_at,
                reason,
            ),
        )

    # ------------------------------------------------------------------ reads
    def get(self, memory_id: str, tenant_id: str) -> VectorMemoryRecord:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT id, tenant_id, user_id, content, metadata, state, version, "
                "embedding_model, embedding_dim, content_sha256, created_at, updated_at, "
                "deleted_at, rolled_back_from, state_reason, schema_version "
                "FROM vector_memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if row is None:
                raise NanoMemoryError(
                    code=ErrorCode.MEMORY_NOT_FOUND,
                    message="Vector memory not found.",
                    details={"id": memory_id},
                )
            record = self._to_record(row)
            acl = conn.execute(
                "SELECT tag FROM memory_acl WHERE memory_id = ? ORDER BY tag", (memory_id,)
            ).fetchall()
        return record.model_copy(update={"acl_tags": tuple(item["tag"] for item in acl)})

    def list(
        self, filter: MemoryFilter, *, limit: int = 50, offset: int = 0
    ) -> list[VectorMemoryRecord]:
        clause, params = filter_sql(filter)
        sql = (
            "SELECT id, tenant_id, user_id, content, metadata, state, version, "
            "embedding_model, embedding_dim, content_sha256, created_at, updated_at, "
            "deleted_at, rolled_back_from, state_reason, schema_version "
            f"FROM vector_memories m WHERE {clause} ORDER BY m.updated_at DESC LIMIT ? OFFSET ?"
        )
        with self.db.reading() as conn:
            rows = conn.execute(sql, [*params, max(0, limit), max(0, offset)]).fetchall()
        return [self._to_record(row) for row in rows]

    def count(self, filter: MemoryFilter) -> int:
        clause, params = filter_sql(filter)
        with self.db.reading() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS total FROM vector_memories m WHERE {clause}", params
            ).fetchone()
        return int(row["total"]) if row else 0

    def candidates(
        self,
        filter: MemoryFilter,
        *,
        embedding_model: str | None = None,
        limit: int = 5000,
    ) -> list[VectorRow]:
        """Return every pre-filtered row with its vector -- the only input to scoring."""
        clause, params = filter_sql(filter)
        sql = (
            "SELECT id, tenant_id, user_id, content, metadata, state, version, embedding, "
            "embedding_model, embedding_dim, content_sha256, created_at, updated_at, "
            "deleted_at, rolled_back_from, state_reason, schema_version "
            f"FROM vector_memories m WHERE {clause} AND m.embedding IS NOT NULL"
        )
        if embedding_model:
            sql += " AND m.embedding_model = ?"
            params = [*params, embedding_model]
        sql += " LIMIT ?"
        params = [*params, max(1, limit)]
        with self.db.reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        result: list[VectorRow] = []
        for row in rows:
            vector = self._vector(row["embedding"], int(row["embedding_dim"]))
            if vector is None:
                continue
            result.append(VectorRow(record=self._to_record(row), vector=vector))
        return result

    def score_all(
        self,
        query_vector: np.ndarray,
        filter: MemoryFilter,
        *,
        embedding_model: str | None = None,
        limit: int = 5000,
    ) -> tuple[list[VectorMemoryRecord], list[float], int]:
        """Dense scores for every pre-filtered candidate (hybrid ranking needs them all)."""
        rows = self.candidates(filter, embedding_model=embedding_model, limit=limit)
        if not rows:
            return [], [], 0
        matrix = np.vstack([row.vector for row in rows])
        query = np.asarray(query_vector, dtype=np.float32)
        if query.shape[0] != matrix.shape[1]:
            raise NanoMemoryError(
                code=ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                message="Query embedding dimension does not match the stored vectors.",
                details={"query_dim": int(query.shape[0]), "stored_dim": int(matrix.shape[1])},
            )
        scores = matrix @ query
        return (
            [row.record for row in rows],
            [max(0.0, min(1.0, float(score))) for score in scores],
            len(rows),
        )

    def versions(self, memory_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.reading() as conn:
            owned = conn.execute(
                "SELECT 1 FROM vector_memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if owned is None:
                raise NanoMemoryError(
                    code=ErrorCode.MEMORY_NOT_FOUND,
                    message="Vector memory not found.",
                    details={"id": memory_id},
                )
            rows = conn.execute(
                "SELECT version, content, metadata, state, acl_tags, created_at, reason "
                "FROM memory_versions WHERE memory_id = ? ORDER BY version DESC",
                (memory_id,),
            ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "content": row["content"],
                "metadata": json.loads(row["metadata"]),
                "state": row["state"],
                "acl_tags": json.loads(row["acl_tags"]),
                "created_at": row["created_at"],
                "reason": row["reason"],
            }
            for row in rows
        ]

    def version_snapshot(
        self, memory_id: str, tenant_id: str, version: int
    ) -> dict[str, Any] | None:
        """Historical snapshot for rollback; ownership is checked before anything is read."""
        with self.db.reading() as conn:
            owned = conn.execute(
                "SELECT 1 FROM vector_memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if owned is None:
                raise NanoMemoryError(
                    code=ErrorCode.MEMORY_NOT_FOUND,
                    message="Vector memory not found.",
                    details={"id": memory_id},
                )
            row = conn.execute(
                "SELECT content, metadata, acl_tags, embedding, embedding_model, "
                "embedding_dim FROM memory_versions WHERE memory_id = ? AND version = ?",
                (memory_id, version),
            ).fetchone()
        if row is None:
            return None
        return {
            "content": row["content"],
            "metadata": json.loads(row["metadata"]),
            "acl_tags": json.loads(row["acl_tags"]),
            "vector": self._vector(row["embedding"], int(row["embedding_dim"])),
            "embedding_model": row["embedding_model"],
            "embedding_dim": int(row["embedding_dim"]),
        }

    # ------------------------------------------------------------------ idempotency
    def idempotent_lookup(self, key: str, tenant_id: str) -> str | None:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT memory_id FROM idempotency WHERE key = ? AND tenant_id = ?",
                (key, tenant_id),
            ).fetchone()
        return str(row["memory_id"]) if row else None

    def idempotent_store(self, key: str, tenant_id: str, memory_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency (key, tenant_id, memory_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, tenant_id, memory_id, utc_now_iso()),
            )

    # ------------------------------------------------------------------ stats
    def global_stats(self) -> dict[str, Any]:
        """Aggregate counts across tenants for the admin/health view (never contents)."""
        with self.db.reading() as conn:
            states = conn.execute(
                "SELECT state, COUNT(*) AS total FROM vector_memories GROUP BY state"
            ).fetchall()
            tenants = conn.execute(
                "SELECT COUNT(DISTINCT tenant_id) AS total FROM vector_memories"
            ).fetchone()["total"]
            traces = conn.execute("SELECT COUNT(*) AS total FROM traces").fetchone()["total"]
            feedback = conn.execute("SELECT COUNT(*) AS total FROM feedback").fetchone()["total"]
        return {
            "tenants": int(tenants or 0),
            "memories": {row["state"]: int(row["total"]) for row in states},
            "traces": int(traces or 0),
            "feedback": int(feedback or 0),
        }

    def stats(self, tenant_id: str) -> dict[str, Any]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS total FROM vector_memories WHERE tenant_id = ? "
                "GROUP BY state",
                (tenant_id,),
            ).fetchall()
            models = conn.execute(
                "SELECT embedding_model, embedding_dim, COUNT(*) AS total "
                "FROM vector_memories WHERE tenant_id = ? GROUP BY embedding_model, embedding_dim",
                (tenant_id,),
            ).fetchall()
        return {
            "backend": self.backend,
            "by_state": {row["state"]: int(row["total"]) for row in rows},
            "total": sum(int(row["total"]) for row in rows),
            "models": [
                {
                    "model": row["embedding_model"],
                    "dim": int(row["embedding_dim"]),
                    "count": int(row["total"]),
                }
                for row in models
            ],
        }


__all__ = ["SQLiteVectorStore", "VectorHits", "VectorRow", "filter_sql"]
