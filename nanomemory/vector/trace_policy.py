"""Trace content policy (security baseline).

The default is **content minimisation**: a trace records what happened (ids, filter shape,
scores, timings, cost, decision flags) but not the user's raw query, the assembled prompt
context or the model response.  Those are the fields that turn an observability table into a
second, ungoverned copy of personal data.

Content retention is opt-in per deployment (NANOMEMORY_TRACE_CONTENT_MODE=full), bounded by
NANOMEMORY_TRACE_CONTENT_MAX_CHARS, and still subject to the retention window that
TraceStore.prune() enforces.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nanomemory.vector.schemas import TraceLog, sha256_hex


class TracePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["minimal", "full"] = "minimal"
    max_field_chars: int = Field(default=4000, ge=0, le=100_000)
    retention_days: int = Field(default=30, ge=1, le=3650)

    @classmethod
    def from_settings(cls, settings: object) -> TracePolicy:
        return cls(
            mode=str(getattr(settings, "trace_content_mode", "minimal")),
            max_field_chars=int(getattr(settings, "trace_content_max_chars", 4000)),
            retention_days=int(getattr(settings, "trace_retention_days", 30)),
        )

    def apply(self, trace: TraceLog) -> TraceLog:
        """Return a copy that is safe to persist under this policy."""
        metadata = dict(trace.metadata)
        metadata["trace_policy"] = self.mode
        if trace.query_raw:
            metadata["query_sha256"] = sha256_hex(trace.query_raw)
            metadata["query_chars"] = len(trace.query_raw)
        if trace.final_prompt_context:
            metadata["context_sha256"] = sha256_hex(trace.final_prompt_context)
            metadata["context_chars"] = len(trace.final_prompt_context)
        if trace.model_response:
            metadata["response_sha256"] = sha256_hex(trace.model_response)
            metadata["response_chars"] = len(trace.model_response)

        if self.mode == "minimal":
            return trace.model_copy(
                update={
                    "query_raw": None,
                    "final_prompt_context": None,
                    "model_response": None,
                    "metadata": metadata,
                }
            )

        def _bounded(value: str | None) -> str | None:
            if not value:
                return None
            return value[: self.max_field_chars]

        return trace.model_copy(
            update={
                "query_raw": _bounded(trace.query_raw),
                "final_prompt_context": _bounded(trace.final_prompt_context),
                "model_response": _bounded(trace.model_response),
                "metadata": metadata,
            }
        )


__all__ = ["TracePolicy"]
