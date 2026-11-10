"""NanoMemory SDK — the ``from nanomemory import Memory`` surface.

Designed for the two-line onboarding in the 2.0 spec::

    memory = Memory()
    await memory.remember("I prefer Python.", user_id="user_001")
    result = await memory.recall("what language do I prefer?", user_id="user_001")

Everything is optional: with no configuration the SDK runs in **lite** mode (deterministic
retrieval, no provider, no API key). Storage is initialised automatically, indexes are
maintained by the engine, and safety/lifecycle defaults are on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from nanomemory.core.config import build_settings
from nanomemory.core.lifecycle import run_lifecycle
from nanomemory.core.memory_formation import MemoryFormation
from nanomemory.core.memory_manager import MemoryManager
from nanomemory.core.memory_models import MemoryType, SourceKind
from nanomemory.providers import BaseProvider

T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from synchronous code (CLI convenience)."""
    return asyncio.run(coro)


class Memory:
    """User-facing memory engine; one instance per storage directory."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        user_id: str = "local",
        session_id: str | None = None,
        mode: str | None = None,
        provider: BaseProvider | None = None,
    ) -> None:
        overrides: dict[str, Any] = {}
        if path is not None:
            overrides["storage_root"] = Path(path).resolve()
        self.settings = build_settings(**overrides)
        self.user_id = user_id
        self.mode = mode or ("standard" if self.settings.provider != "mock" else "lite")
        self.manager = MemoryManager(self.settings, provider=provider, configure_logs=False)
        self.manager.initialize_storage()
        self.manager.startup()
        self.session_id = session_id or self.settings.default_session_id
        if self.session_id not in self.manager.list_sessions():
            self.manager.working.ensure_session(self.session_id)
        self.formation = MemoryFormation(self.settings, self.manager.long_term)

    # ------------------------------------------------------------------ write
    async def remember(
        self,
        content: str,
        *,
        user_id: str | None = None,
        memory_type: MemoryType | str | None = None,
        source: str = "user",
        importance: int | None = None,
        tags: list[str] | None = None,
        confirm: bool | None = None,
    ) -> dict[str, Any]:
        """Extract and store memories from ``content`` (accepts several sentences)."""
        source_kind = (
            SourceKind.USER
            if source in {"user", "sdk", "cli"}
            else SourceKind(source)
            if source in SourceKind._value2member_map_
            else SourceKind.USER
        )
        report = self.formation.form(
            content,
            user_id=user_id or self.user_id,
            source_kind=source_kind,
            source_ref=source,
            session_id=self.session_id,
            confirm=confirm,
        )
        if memory_type is not None and report["items"]:
            wanted = MemoryType(memory_type) if isinstance(memory_type, str) else memory_type
            for item in report["items"]:
                if item.get("memory_type") != wanted.value and "id" in item:
                    try:
                        self.manager.long_term.update(
                            item["id"], {"memory_type": wanted.value}, if_version=None
                        )
                        item["memory_type"] = wanted.value
                    except Exception:  # pragma: no cover - best effort relabel
                        pass
        if tags or importance:
            for item in report["items"]:
                if "id" not in item:
                    continue
                patch: dict[str, Any] = {}
                if tags:
                    patch["tags"] = tags
                if importance is not None:
                    patch["importance"] = max(1, min(5, importance))
                if patch:
                    try:
                        self.manager.long_term.update(item["id"], patch)
                    except Exception:  # pragma: no cover - best effort metadata
                        pass
        report["mode"] = self.mode
        if len(report["items"]) == 1:
            report["memory"] = report["items"][0]
        return report

    # ------------------------------------------------------------------ read
    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        limit: int = 5,
        token_budget: int | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the smallest useful context for ``query``, with per-signal reasons."""
        wanted = None
        if memory_type is not None:
            wanted = MemoryType(memory_type) if isinstance(memory_type, str) else memory_type
        result = self.manager.retriever.retrieve(
            query,
            session_id=self.session_id,
            token_budget=token_budget,
            user_id=user_id or self.user_id,
            memory_type=wanted,
        )
        long_term_ids = [item.id for item in result.selected if item.kind == "long_term"]
        if long_term_ids:
            self.manager.long_term.touch_recalled(long_term_ids)
        items = [
            {
                "id": item.id,
                "kind": item.kind,
                "content": item.content,
                "score": round(item.score, 4),
                "tokens": item.tokens,
                "reason": item.reason,
                "memory_type": item.metadata.get("memory_type"),
                "status": item.metadata.get("status"),
                "signals": item.metadata.get("signals"),
            }
            for item in result.selected
        ][:limit]
        return {
            "query": query,
            "intent": result.intent,
            "insufficient_context": result.insufficient_context,
            "fallback_reason": result.fallback_reason,
            "estimated_tokens": result.estimated_tokens,
            "budget": result.token_budget,
            "items": items,
        }

    async def forget(self, memory_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Archive a memory (reversible); never a hard delete."""
        record = self.manager.long_term.get(memory_id)
        if record.status.value == "pending_confirmation":
            updated = self.manager.long_term.reject(
                memory_id, reason=reason or "forgotten by user", session_id=self.session_id
            )
        else:
            updated = self.manager.long_term.archive(memory_id)
        return {"id": memory_id, "status": updated.status.value}

    async def confirm(self, memory_id: str) -> dict[str, Any]:
        record = self.manager.long_term.confirm(memory_id, session_id=self.session_id)
        return {"id": memory_id, "status": record.status.value}

    async def reject(self, memory_id: str, *, reason: str = "rejected by user") -> dict[str, Any]:
        record = self.manager.long_term.reject(memory_id, reason=reason, session_id=self.session_id)
        return {"id": memory_id, "status": record.status.value}

    async def explain(self, memory_id: str) -> dict[str, Any]:
        """Why does this memory exist, where did it come from, what did it replace?"""
        data = self.manager.long_term.explain_data(memory_id)
        audit: list[dict[str, Any]] = []
        try:
            episodes = self.manager.read_episodes(
                __import__(
                    "nanomemory.core.episodic_memory", fromlist=["EpisodicQuery"]
                ).EpisodicQuery(limit=200)
            )
            for view in episodes.events:
                episode = view.episode
                payload = episode.payload or {}
                if payload.get("memory_id") == memory_id or memory_id in (
                    episode.related_memory_ids or []
                ):
                    audit.append(
                        {
                            "event_id": episode.event_id,
                            "event_type": episode.event_type,
                            "timestamp": episode.timestamp,
                            "outcome": episode.outcome,
                            "payload": payload,
                        }
                    )
        except Exception:  # pragma: no cover - audit is best effort
            audit = []
        data["audit_events"] = audit[-20:]
        return data

    # ------------------------------------------------------------------ ops
    async def lifecycle(self, *, dry_run: bool = True) -> dict[str, Any]:
        return run_lifecycle(self.manager.long_term, dry_run=dry_run)

    async def status(self) -> dict[str, Any]:
        records = self.manager.long_term.list_records(
            limit=500, include_archived=True, include_superseded=True
        )
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for record in records:
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
            by_type[record.memory_type.value] = by_type.get(record.memory_type.value, 0) + 1
        return {
            "mode": self.mode,
            "provider": self.settings.provider,
            "storage_root": str(self.settings.storage_root),
            "user_id": self.user_id,
            "session_id": self.session_id,
            "records": len(records),
            "by_status": by_status,
            "by_type": by_type,
        }

    async def aclose(self) -> None:
        """Nothing to close for file storage; kept for API symmetry."""

    async def __aenter__(self) -> Memory:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = ["Memory"]
