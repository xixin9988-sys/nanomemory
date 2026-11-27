"""Vector store: pre-filtering, versioning and tenant isolation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.db import VectorDatabase
from nanomemory.vector.schemas import (
    MemoryFilter,
    MemoryMetadata,
    VectorMemoryRecord,
    VectorMemoryState,
)
from nanomemory.vector.store import SQLiteVectorStore, filter_sql


def _record(**overrides: object) -> VectorMemoryRecord:
    data: dict[str, object] = {
        "id": "vec-0000000000000001",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "content": "用户偏好深色模式",
        "metadata": MemoryMetadata(category="preference", tags=["ui"]),
        "state": VectorMemoryState.ACTIVE,
    }
    data.update(overrides)
    return VectorMemoryRecord.model_validate(data)


def _vector(dim: int = 4, **values: float) -> np.ndarray:
    vector = np.zeros(dim, dtype=np.float32)
    for index, value in values.items():
        vector[int(index.removeprefix("v"))] = value
    return vector


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteVectorStore:
    db = VectorDatabase(tmp_path / "vectors.db")
    try:
        yield SQLiteVectorStore(db)
    finally:
        db.close()


def test_filter_sql_is_tenant_first_and_user_scoped() -> None:
    filter = MemoryFilter(tenant_id="t1", user_id="u1", acl_tags=("team",))
    sql, params = filter_sql(filter)
    assert sql.startswith("m.tenant_id = ?")
    assert "m.user_id = ?" in sql
    assert "memory_acl" in sql
    assert params[0] == "t1"
    assert "u1" in params
    assert "team" in params


def test_filter_sql_without_acl_has_no_acl_join() -> None:
    filter = MemoryFilter(tenant_id="t1", user_id="u1")
    sql, params = filter_sql(filter)
    assert "memory_acl" not in sql
    assert params[:3] == ["t1", "active", "u1"]
    assert len(params) == 4  # tenant, state, user, now


def test_store_never_returns_another_tenant(store: SQLiteVectorStore) -> None:
    store.insert(_record(), _vector(v0=1.0))
    store.insert(
        _record(id="vec-tenant-b-0001", tenant_id="tenant-b", user_id="user-9"), _vector(v0=1.0)
    )

    filter_a = MemoryFilter(tenant_id="tenant-a", user_id="user-1")
    records, scores, scanned = store.score_all(np.array([1.0, 0, 0, 0], dtype=np.float32), filter_a)
    assert [record.tenant_id for record in records] == ["tenant-a"]
    assert scanned == 1
    assert scores[0] > 0.99


def test_shared_acl_grants_access_but_private_rows_stay_private(
    store: SQLiteVectorStore,
) -> None:
    store.insert(_record(content="private note"), _vector(v0=1.0))
    store.insert(
        _record(
            id="vec-shared-0001", user_id="user-2", content="shared runbook", acl_tags=("team",)
        ),
        _vector(v0=0.9, v1=0.1),
    )
    store.insert(
        _record(
            id="vec-other-0002", user_id="user-3", content="other private", acl_tags=("other-team",)
        ),
        _vector(v0=1.0),
    )
    filter = MemoryFilter(tenant_id="tenant-a", user_id="user-1", acl_tags=("team",))
    records = store.list(filter)
    ids = {record.id for record in records}
    assert ids == {"vec-0000000000000001", "vec-shared-0001"}
    assert "vec-other-0002" not in ids


def test_archived_and_pending_rows_are_not_searchable(store: SQLiteVectorStore) -> None:
    store.insert(_record(), _vector(v0=1.0))
    store.insert(
        _record(id="vec-archived-01", state=VectorMemoryState.ARCHIVED),
        _vector(v0=1.0),
    )
    store.insert(_record(id="vec-pending-001", state=VectorMemoryState.PENDING_EMBEDDING), None)
    records, _, scanned = store.score_all(
        np.array([1.0, 0, 0, 0], dtype=np.float32),
        MemoryFilter(tenant_id="tenant-a", user_id="user-1"),
    )
    assert scanned == 1
    assert records[0].state is VectorMemoryState.ACTIVE


def test_expired_rows_are_excluded_by_default(store: SQLiteVectorStore) -> None:
    store.insert(
        _record(metadata=MemoryMetadata(valid_until="2000-01-01T00:00:00+00:00")),
        _vector(v0=1.0),
    )
    active = MemoryFilter(tenant_id="tenant-a", user_id="user-1")
    assert store.count(active) == 0
    expired = MemoryFilter(tenant_id="tenant-a", user_id="user-1", include_expired=True)
    assert store.count(expired) == 1


def test_versions_and_snapshot_support_rollback(store: SQLiteVectorStore) -> None:
    first = _record(content="版本一")
    store.insert(first, _vector(v0=1.0))
    second = first.model_copy(update={"content": "版本二", "version": 2, "content_sha256": ""})
    second = VectorMemoryRecord.model_validate(second.model_dump(mode="json"))
    store.save(second, _vector(v0=0.5, v1=0.5), reason="updated")

    versions = store.versions(first.id, "tenant-a")
    assert [item["version"] for item in versions] == [2, 1]
    snapshot = store.version_snapshot(first.id, "tenant-a", 1)
    assert snapshot is not None
    assert snapshot["content"] == "版本一"
    assert snapshot["vector"] is not None


def test_cross_tenant_get_is_indistinguishable_from_missing(store: SQLiteVectorStore) -> None:
    store.insert(_record(), _vector(v0=1.0))
    with pytest.raises(NanoMemoryError) as excinfo:
        store.get("vec-0000000000000001", "tenant-b")
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND
    assert excinfo.value.status == 404


def test_duplicate_id_is_rejected(store: SQLiteVectorStore) -> None:
    record = _record()
    store.insert(record, _vector(v0=1.0))
    with pytest.raises(NanoMemoryError) as excinfo:
        store.insert(record, _vector(v0=1.0))
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
