"""Repository contract: the behaviour every vector backend must preserve.

Today the only adapter is SQLite.  When a pgvector adapter is added, it is added to the
parameterisation here first; a backend that cannot pass these tests is not selectable.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.repository import VectorMemoryRepository
from nanomemory.vector.schemas import MemoryFilter, MemoryMetadata, VectorMemoryRecord
from nanomemory.vector.store import SQLiteVectorStore

TENANT = "tenant-a"
USER = "user-1"


@pytest.fixture(params=["sqlite"])
def repository(request: pytest.FixtureRequest, tmp_path: Path):
    assert request.param == "sqlite"
    database = VectorDatabase(tmp_path / "vectors.db")
    try:
        yield SQLiteVectorStore(database)
    finally:
        database.close()


def _record(memory_id: str, **overrides) -> VectorMemoryRecord:
    data = {
        "id": memory_id,
        "tenant_id": TENANT,
        "user_id": USER,
        "content": f"memory {memory_id}",
        "metadata": MemoryMetadata(category="fact", tags=["contract"]),
        "state": "active",
        "embedding_model": "contract-4",
        "embedding_dim": 4,
    }
    data.update(overrides)
    return VectorMemoryRecord.model_validate(data)


def _vector() -> np.ndarray:
    return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


def test_adapter_satisfies_the_protocol(repository) -> None:
    assert isinstance(repository, VectorMemoryRepository)


def test_tenant_scope_is_enforced_on_read_and_score(repository) -> None:
    repository.insert(_record("vec-contract-01"), _vector())
    repository.insert(_record("vec-contract-02", tenant_id="tenant-b"), _vector())

    filter = MemoryFilter(tenant_id=TENANT, user_id=USER)
    assert [item.id for item in repository.list(filter)] == ["vec-contract-01"]
    records, scores, scanned = repository.score_all(_vector(), filter)
    assert scanned == 1
    assert [item.id for item in records] == ["vec-contract-01"]

    with pytest.raises(NanoMemoryError) as excinfo:
        repository.get("vec-contract-02", TENANT)
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND


def test_pending_rows_are_never_candidates(repository) -> None:
    repository.insert(_record("vec-contract-03"), None)
    filter = MemoryFilter(tenant_id=TENANT, user_id=USER)
    assert repository.count(filter) == 1
    records, _, scanned = repository.score_all(_vector(), filter)
    assert records == []
    assert scanned == 0


def test_versions_are_append_only(repository) -> None:
    first = _record("vec-contract-04")
    repository.insert(first, _vector())
    second = first.model_copy(update={"content": "memory v2", "version": 2})
    repository.save(second, _vector(), reason="updated")
    versions = repository.versions(first.id, TENANT)
    assert [item["version"] for item in versions] == [2, 1]
    snapshot = repository.version_snapshot(first.id, TENANT, 1)
    assert snapshot is not None
    assert snapshot["content"] == first.content


def test_idempotency_key_is_tenant_scoped(repository) -> None:
    repository.idempotent_store("key-1", TENANT, "vec-contract-05")
    assert repository.idempotent_lookup("key-1", TENANT) == "vec-contract-05"
    assert repository.idempotent_lookup("key-1", "tenant-b") is None


def test_acl_join_is_applied_before_scoring(repository) -> None:
    repository.insert(_record("vec-contract-06"), _vector())
    repository.insert(_record("vec-contract-07", user_id="user-2", acl_tags=("team",)), _vector())
    private = MemoryFilter(tenant_id=TENANT, user_id=USER)
    shared = MemoryFilter(tenant_id=TENANT, user_id=USER, acl_tags=("team",))
    assert {item.id for item in repository.list(private)} == {"vec-contract-06"}
    assert {item.id for item in repository.list(shared)} == {
        "vec-contract-06",
        "vec-contract-07",
    }
