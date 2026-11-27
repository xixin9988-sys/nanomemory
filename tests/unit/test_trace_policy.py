"""Trace privacy policy and retention pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanomemory.core.config import build_settings
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.schemas import MemoryMetadata, TenantContext, TraceLog
from nanomemory.vector.service import NewMemory, VectorMemoryService
from nanomemory.vector.trace import TraceStore


def _settings(tmp_path: Path, **overrides):
    base = {
        "storage_root": tmp_path / "storage",
        "environment": "test",
        "embedding_dimension": 32,
        "embedding_max_retries": 0,
        "log_level": "ERROR",
    }
    base.update(overrides)
    return build_settings(**base)


def test_minimal_policy_stores_hashes_not_text(tmp_path: Path) -> None:
    service = VectorMemoryService(_settings(tmp_path, trace_content_mode="minimal"))
    service.ensure_ready()
    context = TenantContext(tenant_id="tenant-a", user_id="user-1")

    async def run():
        await service.remember(
            context,
            NewMemory(content="用户偏好深色模式", metadata=MemoryMetadata(category="preference")),
        )
        return await service.search(context, "深色模式")

    result = __import__("asyncio").run(run())
    trace = service.trace(context, result.trace_id)
    assert trace.query_raw is None
    assert trace.final_prompt_context is None
    assert trace.metadata["trace_policy"] == "minimal"
    assert len(trace.metadata["query_sha256"]) == 64
    assert trace.metadata["query_chars"] == len("深色模式")
    assert trace.retrieved_chunk_ids
    service.close()


def test_full_policy_keeps_bounded_text(tmp_path: Path) -> None:
    service = VectorMemoryService(
        _settings(tmp_path, trace_content_mode="full", trace_content_max_chars=4)
    )
    service.ensure_ready()
    context = TenantContext(tenant_id="tenant-a", user_id="user-1")

    async def run():
        await service.remember(
            context, NewMemory(content="订单 A1001 已签收", metadata=MemoryMetadata())
        )
        return await service.search(context, "订单 A1001")

    result = __import__("asyncio").run(run())
    trace = service.trace(context, result.trace_id)
    assert trace.query_raw == "订单 A"
    assert trace.metadata["trace_policy"] == "full"
    service.close()


def test_prune_deletes_only_old_traces_of_one_tenant(tmp_path: Path) -> None:
    database = VectorDatabase(tmp_path / "vectors.db")
    store = TraceStore(database)
    old = datetime.now(UTC) - timedelta(days=40)
    new = datetime.now(UTC) - timedelta(days=2)
    for trace_id, created, tenant in (
        ("tr-old-a", old, "tenant-a"),
        ("tr-new-a", new, "tenant-a"),
        ("tr-old-b", old, "tenant-b"),
    ):
        store.record(
            TraceLog(
                trace_id=trace_id,
                kind="search",
                tenant_id=tenant,
                user_id="user-1",
                total_latency_ms=1.0,
                created_at=created.isoformat(),
            )
        )
    result = store.prune("tenant-a", 30)
    assert result["traces"] == 1
    assert store.stats("tenant-a")["traces"] == 1
    assert store.stats("tenant-b")["traces"] == 1
    database.close()
