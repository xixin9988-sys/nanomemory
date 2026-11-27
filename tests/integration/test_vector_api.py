"""Vector memory HTTP contract: lifecycle, isolation, signed context, feedback."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from nanomemory.api.main import create_app
from nanomemory.core.config import build_settings
from nanomemory.vector.context import sign_headers

H = {"X-NM-Tenant": "tenant-a", "X-NM-User": "user-1"}


def _client(tmp_path: Path, **overrides):
    settings = build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        embedding_dimension=32,
        rate_limit_enabled=False,
        log_level="ERROR",
        **overrides,
    )
    app = create_app(settings, configure_logs=False)
    return TestClient(app), settings


def _create(client: TestClient, content: str, headers=H, **extra):
    body = {"content": content, "category": "preference", "tags": ["ui"]}
    body.update(extra)
    return client.post("/api/v1/memory/vectors", headers=headers, json=body)


def test_create_search_and_trace(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        created = _create(client, "用户偏好深色模式和大字体")
        assert created.status_code == 201, created.text
        payload = created.json()
        memory_id = payload["record"]["id"]
        assert created.headers["X-NanoMemory-Trace-Id"].startswith("tr-")

        search = client.post(
            "/api/v1/memory/vectors/search", headers=H, json={"query": "深色模式", "limit": 3}
        )
        assert search.status_code == 200
        body = search.json()
        assert body["items"][0]["id"] == memory_id
        assert body["insufficient_context"] is False
        assert body["timings_ms"]["total"] >= 0

        trace = client.get(f"/api/v1/traces/{body['trace_id']}", headers=H)
        assert trace.status_code == 200
        trace_body = trace.json()
        assert trace_body["retrieved_chunk_ids"] == [memory_id]
        assert trace_body["filter_applied"]["tenant_id"] == "tenant-a"


def test_scope_cannot_be_smuggled_in_the_body(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        response = _create(client, "hello", tenant_id="tenant-b")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_failed"


def test_other_tenants_see_nothing(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        _create(client, "用户偏好深色模式")
        other = client.post(
            "/api/v1/memory/vectors/search",
            headers={"X-NM-Tenant": "tenant-b", "X-NM-User": "user-9"},
            json={"query": "深色模式"},
        )
        assert other.status_code == 200
        assert other.json()["items"] == []
        assert other.json()["candidate_count"] == 0
        listing = client.get(
            "/api/v1/memory/vectors",
            headers={"X-NM-Tenant": "tenant-b", "X-NM-User": "user-9"},
        )
        assert listing.json()["records"] == []


def test_lifecycle_update_rollback_reactivate(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        memory_id = _create(client, "深色模式 第一版").json()["record"]["id"]
        patched = client.patch(
            f"/api/v1/memory/vectors/{memory_id}",
            headers=H,
            json={"content": "浅色模式 第二版", "expected_version": 1},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["record"]["version"] == 2

        versions = client.get(f"/api/v1/memory/vectors/{memory_id}/versions", headers=H)
        assert [item["version"] for item in versions.json()["versions"]] == [2, 1]

        rollback = client.post(
            f"/api/v1/memory/vectors/{memory_id}/rollback",
            headers=H,
            json={"target_version": 1},
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["record"]["state"] == "rolled_back"
        assert rollback.json()["record"]["content"] == "深色模式 第一版"

        search = client.post("/api/v1/memory/vectors/search", headers=H, json={"query": "深色模式"})
        assert all(item["id"] != memory_id for item in search.json()["items"])

        active = client.post(f"/api/v1/memory/vectors/{memory_id}/reactivate", headers=H)
        assert active.json()["record"]["state"] == "active"


def test_version_conflict_is_409(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        memory_id = _create(client, "第一版").json()["record"]["id"]
        conflict = client.patch(
            f"/api/v1/memory/vectors/{memory_id}",
            headers=H,
            json={"content": "第二版", "expected_version": 99},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "version_conflict"


def test_delete_and_list(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        memory_id = _create(client, "会被删除的记忆").json()["record"]["id"]
        deleted = client.delete(f"/api/v1/memory/vectors/{memory_id}", headers=H)
        assert deleted.json()["record"]["state"] == "deleted"
        listing = client.get("/api/v1/memory/vectors", headers=H)
        assert listing.json()["records"] == []
        everything = client.get("/api/v1/memory/vectors", headers=H, params={"state": "deleted"})
        assert everything.json()["count"] == 1


def test_feedback_links_to_trace_and_exports(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path, admin_token=SecretStr("admin-secret"))
    with client:
        _create(client, "用户偏好深色模式")
        search = client.post("/api/v1/memory/vectors/search", headers=H, json={"query": "深色模式"})
        trace_id = search.json()["trace_id"]
        feedback = client.post(
            "/api/v1/feedback",
            headers=H,
            json={"trace_id": trace_id, "verdict": "down", "reason": "wrong_memory"},
        )
        assert feedback.status_code == 200
        assert feedback.json()["feedback"]["trace_id"] == trace_id

        export = client.get(
            "/api/v1/feedback/export",
            headers={**H, "Authorization": "Bearer admin-secret"},
        )
        assert export.status_code == 200, export.text
        assert export.json()["exported"] == 1

        unknown = client.post(
            "/api/v1/feedback",
            headers=H,
            json={"trace_id": "tr-unknown", "verdict": "up"},
        )
        assert unknown.status_code == 404


def test_signed_context_is_enforced(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path, context_signing_key=SecretStr("shared-secret"))
    with client:
        unsigned = _create(client, "未签名的请求")
        assert unsigned.status_code == 401
        assert unsigned.json()["code"] == "context_required"

        headers = sign_headers(tenant_id="tenant-a", user_id="user-1", secret="shared-secret")
        signed = _create(client, "已签名的请求", headers=headers)
        assert signed.status_code == 201, signed.text

        tampered = dict(headers)
        tampered["X-NM-User"] = "user-2"
        forged = _create(client, "伪造的请求", headers=tampered)
        assert forged.status_code == 401
        assert forged.json()["code"] == "context_signature_invalid"


def test_idempotency_key_replays(tmp_path: Path) -> None:
    client, _settings = _client(tmp_path)
    with client:
        headers = {**H, "Idempotency-Key": "abc-123"}
        first = _create(client, "幂等写入", headers=headers)
        second = _create(client, "幂等写入", headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert second.headers.get("Idempotent-Replay") == "true"
        assert first.json()["record"]["id"] == second.json()["record"]["id"]
        listing = client.get("/api/v1/memory/vectors", headers=H)
        assert listing.json()["count"] == 1
