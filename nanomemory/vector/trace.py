"""Trace and feedback storage: the observability spine of vector memory.

Every search/write call produces one TraceLog row.  Feedback rows point at a trace, and
badcase export turns a thumbs-down into a candidate eval case without copy/paste.  This is
what closes the loop in standard #4 instead of leaving it as a dashboard promise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.schemas import FeedbackRecord, TraceLog, utc_now_iso


def _loads(value: str | None) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:  # pragma: no cover - defensive, rows are written by us
        return None


class TraceStore:
    """Append-only-ish trace log plus feedback ledger."""

    def __init__(self, database: VectorDatabase) -> None:
        self.db = database

    # ------------------------------------------------------------------ traces
    def record(self, trace: TraceLog) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO traces (trace_id, kind, tenant_id, user_id, "
                "request_id, query_raw, filter_applied, retrieved_chunk_ids, "
                "similarity_scores, final_prompt_context, model_response, ttft_ms, "
                "total_latency_ms, input_tokens, output_tokens, estimated_cost, "
                "embedding_model, insufficient_context, degraded, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace.trace_id,
                    trace.kind,
                    trace.tenant_id,
                    trace.user_id,
                    trace.request_id,
                    trace.query_raw,
                    json.dumps(trace.filter_applied, ensure_ascii=False),
                    json.dumps(trace.retrieved_chunk_ids),
                    json.dumps(trace.similarity_scores),
                    trace.final_prompt_context,
                    trace.model_response,
                    trace.ttft_ms,
                    trace.total_latency_ms,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.estimated_cost,
                    trace.embedding_model,
                    int(trace.insufficient_context),
                    int(trace.degraded),
                    json.dumps(trace.metadata, ensure_ascii=False),
                    trace.created_at,
                ),
            )

    def get(self, trace_id: str, *, tenant_id: str | None = None) -> TraceLog:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        if row is None or (tenant_id is not None and row["tenant_id"] != tenant_id):
            # A missing trace and a foreign trace are indistinguishable on purpose.
            raise NanoMemoryError(
                code=ErrorCode.NOT_FOUND,
                message="Trace not found.",
                details={"trace_id": trace_id},
            )
        return TraceLog(
            trace_id=row["trace_id"],
            kind=row["kind"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            request_id=row["request_id"],
            query_raw=row["query_raw"],
            filter_applied=_loads(row["filter_applied"]) or {},
            retrieved_chunk_ids=_loads(row["retrieved_chunk_ids"]) or [],
            similarity_scores=_loads(row["similarity_scores"]) or [],
            final_prompt_context=row["final_prompt_context"],
            model_response=row["model_response"],
            ttft_ms=row["ttft_ms"],
            total_latency_ms=float(row["total_latency_ms"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            estimated_cost=float(row["estimated_cost"]),
            embedding_model=row["embedding_model"],
            insufficient_context=bool(row["insufficient_context"]),
            degraded=bool(row["degraded"]),
            metadata=_loads(row["metadata"]) or {},
            created_at=row["created_at"],
        )

    def list(self, tenant_id: str, *, limit: int = 50, offset: int = 0) -> list[TraceLog]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM traces WHERE tenant_id = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, max(0, limit), max(0, offset)),
            ).fetchall()
        return [self.get(row["trace_id"], tenant_id=tenant_id) for row in rows]

    # ------------------------------------------------------------------ feedback
    def add_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        # The trace must exist before feedback is accepted: an orphan rating is not a signal.
        self.get(feedback.trace_id, tenant_id=feedback.tenant_id)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO feedback (feedback_id, trace_id, verdict, reason, comment, "
                "tenant_id, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback.feedback_id,
                    feedback.trace_id,
                    feedback.verdict,
                    feedback.reason,
                    feedback.comment,
                    feedback.tenant_id,
                    feedback.user_id,
                    feedback.created_at,
                ),
            )
        return feedback

    def list_feedback(self, tenant_id: str, *, limit: int = 100) -> list[FeedbackRecord]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, max(0, limit)),
            ).fetchall()
        return [
            FeedbackRecord(
                feedback_id=row["feedback_id"],
                trace_id=row["trace_id"],
                verdict=row["verdict"],
                reason=row["reason"],
                comment=row["comment"],
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def badcases(self, tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Thumbs-down traces rendered as eval candidates."""
        cases: list[dict[str, Any]] = []
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT f.*, t.query_raw, t.retrieved_chunk_ids, t.similarity_scores, "
                "t.insufficient_context FROM feedback f JOIN traces t ON t.trace_id = f.trace_id "
                "WHERE f.tenant_id = ? AND f.verdict = 'down' "
                "ORDER BY f.created_at DESC LIMIT ?",
                (tenant_id, max(0, limit)),
            ).fetchall()
        for row in rows:
            hit_ids = _loads(row["retrieved_chunk_ids"]) or []
            cases.append(
                {
                    "id": f"badcase-{row['trace_id']}",
                    "type": "regression",
                    "query": row["query_raw"] or "",
                    "expected_memory_ids": [],
                    "forbidden_memory_ids": hit_ids[:3],
                    "expected_behaviour": "answer" if hit_ids else "refuse",
                    "notes": (
                        f"user feedback: {row['reason'] or 'unspecified'}"
                        + (f" ({row['comment']})" if row["comment"] else "")
                    ),
                    "source_trace_id": row["trace_id"],
                    "scores": _loads(row["similarity_scores"]) or [],
                }
            )
        return cases

    def export_badcases(self, tenant_id: str, path: Path) -> int:
        """Append deduplicated badcases to an eval dataset JSONL file."""
        existing: set[str] = set()
        path = Path(path)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    existing.add(str(json.loads(line).get("id")))
                except json.JSONDecodeError:
                    continue
        cases = [case for case in self.badcases(tenant_id) if case["id"] not in existing]
        if not cases:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
        return len(cases)

    def prune(self, tenant_id: str, older_than_days: int) -> dict[str, int]:
        """Delete traces (and their feedback) older than the tenant's retention window."""
        if older_than_days < 1:
            raise NanoMemoryError(
                code=ErrorCode.VALIDATION_FAILED,
                message="older_than_days must be >= 1",
            )
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self.db.transaction() as conn:
            trace_ids = [
                row["trace_id"]
                for row in conn.execute(
                    "SELECT trace_id FROM traces WHERE tenant_id = ? AND created_at < ?",
                    (tenant_id, cutoff),
                ).fetchall()
            ]
            if not trace_ids:
                return {"traces": 0, "feedback": 0}
            placeholders = ",".join("?" * len(trace_ids))
            feedback = conn.execute(
                f"DELETE FROM feedback WHERE trace_id IN ({placeholders})", trace_ids
            ).rowcount
            traces = conn.execute(
                f"DELETE FROM traces WHERE trace_id IN ({placeholders})", trace_ids
            ).rowcount
        return {"traces": int(traces or 0), "feedback": int(feedback or 0)}

    # ------------------------------------------------------------------ stats
    def stats(self, tenant_id: str) -> dict[str, Any]:
        with self.db.reading() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM traces WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()["n"]
            refused = conn.execute(
                "SELECT COUNT(*) AS n FROM traces WHERE tenant_id = ? AND insufficient_context = 1",
                (tenant_id,),
            ).fetchone()["n"]
            latency = conn.execute(
                "SELECT AVG(total_latency_ms) AS avg FROM traces WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()["avg"]
            feedback = conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM feedback WHERE tenant_id = ? GROUP BY verdict",
                (tenant_id,),
            ).fetchall()
            exported_at = utc_now_iso()
        return {
            "traces": int(total or 0),
            "refusal_rate": round(int(refused or 0) / total, 4) if total else 0.0,
            "avg_latency_ms": round(float(latency or 0.0), 2),
            "feedback": {row["verdict"]: int(row["n"]) for row in feedback},
            "as_of": exported_at,
        }


__all__ = ["TraceStore"]
