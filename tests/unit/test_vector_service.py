"""Vector memory service: lifecycle, failures, tenant scope and traces."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.providers.base import ProviderError, ProviderErrorCategory
from nanomemory.vector.embeddings import EmbeddingProvider
from nanomemory.vector.schemas import (
    EmbeddingResult,
    MemoryMetadata,
    TenantContext,
    VectorMemoryState,
)
from nanomemory.vector.service import NewMemory, VectorMemoryService

DIM = 32


class FixedProvider(EmbeddingProvider):
    """Deterministic 32-dim provider that can be switched into failure mode."""

    provider_name = "fixed"
    model_name = "fixed-32"
    dim = DIM

    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        if self.fail:
            raise ProviderError(ProviderErrorCategory.TIMEOUT, "provider down", retryable=False)
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0001] * DIM
            for index, char in enumerate(text[:DIM]):
                vector[index % DIM] += float(ord(char) % 17)
            norm = sum(value * value for value in vector) ** 0.5
            vectors.append([value / norm for value in vector])
        return EmbeddingResult(
            vectors=vectors, model=self.model_name, dim=DIM, tokens=len(texts) * 5
        )


@pytest.fixture()
def service(tmp_path: Path):
    settings = build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        embedding_dimension=DIM,
        embedding_max_retries=0,
        log_level="ERROR",
    )
    provider = FixedProvider()
    svc = VectorMemoryService(settings, provider=provider)
    svc.ensure_ready()
    yield svc, provider
    svc.close()


def ctx(user: str = "user-1", tenant: str = "tenant-a", acl=()) -> TenantContext:
    return TenantContext(tenant_id=tenant, user_id=user, acl_tags=acl)


def payload(content: str = "用户偏好深色模式", acl=()) -> NewMemory:
    return NewMemory(
        content=content,
        metadata=MemoryMetadata(category="preference", tags=["ui"]),
        acl_tags=acl,
    )


async def test_remember_activates_and_traces(service) -> None:
    svc, provider = service
    outcome = await svc.remember(ctx(), payload())
    assert outcome.record.state is VectorMemoryState.ACTIVE
    assert outcome.record.embedding_dim == DIM
    trace = svc.trace(ctx(), outcome.trace_id)
    assert trace.kind == "write"
    assert trace.tenant_id == "tenant-a"
    assert trace.embedding_model == "fixed-32"


async def test_embedding_failure_leaves_pending_then_retry_succeeds(service) -> None:
    svc, provider = service
    provider.fail = True
    with pytest.raises(NanoMemoryError) as excinfo:
        await svc.remember(ctx(), payload())
    assert excinfo.value.code is ErrorCode.EMBEDDING_FAILED
    memory_id = excinfo.value.details["memory_id"]

    pending = svc.get(ctx(), memory_id)
    assert pending.state is VectorMemoryState.PENDING_EMBEDDING
    assert "embedding_failed" in (pending.state_reason or "")
    # A pending row is invisible to retrieval.
    result = await svc.search(ctx(), "深色模式")
    assert result.items == []
    assert result.insufficient_context is True

    provider.fail = False
    outcome = await svc.retry_embedding(ctx(), memory_id)
    assert outcome.record.state is VectorMemoryState.ACTIVE


async def test_update_is_atomic_when_embedding_fails(service) -> None:
    svc, provider = service
    created = await svc.remember(ctx(), payload())
    provider.fail = True
    with pytest.raises(NanoMemoryError) as excinfo:
        await svc.update(ctx(), created.record.id, content="全新的内容")
    assert excinfo.value.code is ErrorCode.EMBEDDING_FAILED
    unchanged = svc.get(ctx(), created.record.id)
    assert unchanged.content == created.record.content
    assert unchanged.version == created.record.version
    assert unchanged.state is VectorMemoryState.ACTIVE


async def test_update_creates_a_version_history(service) -> None:
    svc, _provider = service
    created = await svc.remember(ctx(), payload("版本一"))
    updated = await svc.update(ctx(), created.record.id, content="版本二", expected_version=1)
    assert updated.record.version == 2
    versions = svc.versions(ctx(), created.record.id)
    assert [item["version"] for item in versions] == [2, 1]


async def test_rollback_requires_reactivation(service) -> None:
    svc, _provider = service
    created = await svc.remember(ctx(), payload("深色模式 第一版"))
    await svc.update(ctx(), created.record.id, content="浅色模式 第二版")
    rolled = await svc.rollback(ctx(), created.record.id, target_version=1)
    assert rolled.record.state is VectorMemoryState.ROLLED_BACK
    assert rolled.record.content == "深色模式 第一版"
    assert rolled.record.rolled_back_from == 1

    # Not retrievable while rolled back.
    result = await svc.search(ctx(), "深色模式")
    assert all(hit.id != created.record.id for hit in result.items)

    active = svc.reactivate(ctx(), created.record.id)
    assert active.state is VectorMemoryState.ACTIVE
    result = await svc.search(ctx(), "深色模式")
    assert any(hit.id == created.record.id for hit in result.items)


async def test_acl_escalation_is_rejected(service) -> None:
    svc, _provider = service
    with pytest.raises(NanoMemoryError) as excinfo:
        await svc.remember(ctx(), payload(acl=("team",)))
    assert excinfo.value.code is ErrorCode.TENANT_SCOPE_VIOLATION
    outcome = await svc.remember(ctx(acl=("team",)), payload(acl=("team",)))
    assert outcome.record.acl_tags == ("team",)


async def test_search_refuses_on_empty_corpus(service) -> None:
    svc, _provider = service
    result = await svc.search(ctx(), "退款政策是什么")
    assert result.items == []
    assert result.insufficient_context is True
    assert "floor" in (result.fallback_reason or "")


async def test_search_degrades_to_lexical_when_embedding_fails(service) -> None:
    svc, provider = service
    await svc.remember(ctx(), payload("订单 A1001 由顺丰快递发出"))
    provider.fail = True
    result = await svc.search(ctx(), "A1001 顺丰")
    assert result.degraded is True
    assert "embedding_unavailable" in (result.fallback_reason or "")
    assert result.items, "lexical fallback should still find the order"
    trace = svc.trace(ctx(), result.trace_id)
    assert trace.degraded is True


async def test_cross_tenant_reads_are_denied(service) -> None:
    svc, _provider = service
    created = await svc.remember(ctx(), payload())
    with pytest.raises(NanoMemoryError) as excinfo:
        svc.get(ctx(tenant="tenant-b"), created.record.id)
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND


async def test_idempotency_replay_returns_the_same_record(service) -> None:
    svc, provider = service
    first = await svc.remember(ctx(), payload(), idempotency_key="key-1")
    calls = provider.calls
    second = await svc.remember(ctx(), payload(), idempotency_key="key-1")
    assert second.idempotent_replay is True
    assert second.record.id == first.record.id
    assert provider.calls == calls, "a replay must not spend another embedding call"


async def test_feedback_requires_a_known_trace(service) -> None:
    svc, _provider = service
    with pytest.raises(NanoMemoryError) as excinfo:
        svc.feedback(ctx(), trace_id="tr-does-not-exist", verdict="down")
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    outcome = await svc.remember(ctx(), payload())
    feedback = svc.feedback(ctx(), trace_id=outcome.trace_id, verdict="down", reason="wrong_memory")
    assert feedback.trace_id == outcome.trace_id
    assert svc.traces.stats("tenant-a")["feedback"] == {"down": 1}


async def test_delete_is_soft_and_terminal(service) -> None:
    svc, _provider = service
    created = await svc.remember(ctx(), payload())
    deleted = svc.delete(ctx(), created.record.id)
    assert deleted.state is VectorMemoryState.DELETED
    with pytest.raises(NanoMemoryError) as excinfo:
        svc.reactivate(ctx(), created.record.id)
    assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION


async def test_badcase_export_is_deduplicated(service, tmp_path: Path) -> None:
    svc, _provider = service
    outcome = await svc.remember(ctx(), payload())
    svc.feedback(ctx(), trace_id=outcome.trace_id, verdict="down", reason="wrong_memory")
    path = tmp_path / "badcases.jsonl"
    assert svc.export_badcases(ctx(), path) == 1
    assert svc.export_badcases(ctx(), path) == 0
    content = path.read_text(encoding="utf-8")
    assert outcome.trace_id in content
