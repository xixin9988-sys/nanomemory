"""API integration tests (spec §12, §22.21, §24.4, §28.2, §30.3)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nanomemory.api.main import create_app
from nanomemory.core.config import build_settings
from nanomemory.core.memory_manager import MemoryManager
from nanomemory.providers.mock_provider import MockProvider

ADMIN_TOKEN = "test-admin-token-value"


def _manager(tmp_path, **overrides):
    settings = build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        provider="mock",
        log_level="WARNING",
        **overrides,
    )
    manager = MemoryManager(settings, provider=MockProvider(), configure_logs=False)
    return settings, manager


@pytest.fixture()
def api(tmp_path):
    settings, manager = _manager(tmp_path)
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        yield client, manager


@pytest.fixture()
def admin_api(tmp_path):
    settings, manager = _manager(tmp_path, admin_token=SecretStr(ADMIN_TOKEN))
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        yield client, manager


def test_health_ok(api) -> None:
    client, _manager = api
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["provider_mode"] == "mock"
    assert payload["context_token_budget"] == 8000
    assert payload["memory_token_limit"] == 6400
    assert response.headers["X-NanoMemory-Request-ID"]
    assert "session_mode" in payload


def test_health_never_leaks_storage_path(api) -> None:
    client, manager = api
    payload = client.get("/health").json()
    assert str(manager.settings.storage_root) not in str(payload)


def test_get_working_memory(api) -> None:
    client, _manager = api
    response = client.get("/api/v1/memory/working")
    assert response.status_code == 200
    payload = response.json()
    assert payload["memory_version"] == 1
    assert payload["document"]["status"] == "idle"


def test_put_working_memory_and_conflict(api) -> None:
    client, _manager = api
    first = client.put(
        "/api/v1/memory/working",
        json={"expected_version": 1, "current_goal": "API goal", "status": "in_progress"},
    )
    assert first.status_code == 200
    assert first.json()["memory_version"] == 2
    stale = client.put(
        "/api/v1/memory/working",
        json={"expected_version": 1, "current_goal": "stale"},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "version_conflict"
    assert body["details"]["current_version"] == 2
    assert body["request_id"]


def test_working_memory_requires_expected_version(api) -> None:
    client, _manager = api
    response = client.put("/api/v1/memory/working", json={"current_goal": "no version"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


def test_unknown_route_404_envelope(api) -> None:
    client, _manager = api
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_method_not_allowed(api) -> None:
    client, _manager = api
    response = client.patch("/health")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_payload_too_large(tmp_path) -> None:
    settings, manager = _manager(tmp_path, request_max_bytes=50)
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/memory/long-term",
            json={"content": "x" * 500, "category": "test"},
        )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_secret_in_long_term_rejected(api) -> None:
    client, _manager = api
    response = client.post(
        "/api/v1/memory/long-term",
        json={
            "content": "key sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
            "category": "test",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "secret_detected"


def test_long_term_crud(api) -> None:
    client, _manager = api
    created = client.post(
        "/api/v1/memory/long-term",
        json={
            "content": "The API supports optimistic concurrency.",
            "category": "api",
            "tags": ["api"],
        },
    )
    assert created.status_code == 200
    record = created.json()
    assert record["version"] == 1
    listed = client.get("/api/v1/memory/long-term", params={"category": "api"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    patched = client.patch(
        f"/api/v1/memory/long-term/{record['id']}",
        json={"if_version": 1, "content": "Updated API fact."},
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2
    stale = client.patch(
        f"/api/v1/memory/long-term/{record['id']}",
        json={"if_version": 1, "content": "Stale update."},
    )
    assert stale.status_code == 409
    deleted = client.delete(f"/api/v1/memory/long-term/{record['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["permanent"] is False


def test_episode_append_and_list(api) -> None:
    client, _manager = api
    created = client.post(
        "/api/v1/memory/episodes",
        json={"event_type": "decision_made", "summary": "API test decision"},
    )
    assert created.status_code == 200
    event_id = created.json()["event_id"]
    listed = client.get("/api/v1/memory/episodes")
    assert listed.status_code == 200
    ids = [item["episode"]["event_id"] for item in listed.json()["events"]]
    assert event_id in ids


def test_agent_run_mock(api) -> None:
    client, _manager = api
    response = client.post("/api/v1/agent/run", json={"task": "do something useful"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"completed", "in_progress"}
    assert payload["provider"] == "mock"
    assert payload["usage"][0]["usage_source"] == "mock"


def test_metrics_endpoints(api) -> None:
    client, _manager = api
    client.post("/api/v1/agent/run", json={"task": "generate a metric"})
    usage = client.get("/api/v1/metrics/usage")
    assert usage.status_code == 200
    assert usage.json()["count"] >= 1
    summary = client.get("/api/v1/metrics/summary")
    assert summary.status_code == 200
    assert summary.json()["requests"] >= 1


def test_single_session_mode_rejects_session_creation(api) -> None:
    client, _manager = api
    response = client.post("/api/v1/sessions")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_multi_session_mode_requires_header(tmp_path) -> None:
    settings, manager = _manager(tmp_path, session_mode="multi")
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        created = client.post("/api/v1/sessions")
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        assert uuid.UUID(session_id).version == 4

        missing = client.get("/api/v1/memory/working")
        assert missing.status_code == 400
        assert missing.json()["code"] == "session_id_required"

        invalid = client.get(
            "/api/v1/memory/working", headers={"X-NanoMemory-Session-ID": "not-a-uuid"}
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "session_id_invalid"

        unknown = client.get(
            "/api/v1/memory/working",
            headers={"X-NanoMemory-Session-ID": str(uuid.uuid4())},
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "session_not_found"

        ok = client.get("/api/v1/memory/working", headers={"X-NanoMemory-Session-ID": session_id})
        assert ok.status_code == 200
        assert ok.json()["session_id"] == session_id


def test_session_isolation(tmp_path) -> None:
    settings, manager = _manager(tmp_path, session_mode="multi")
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        first = client.post("/api/v1/sessions").json()["session_id"]
        second = client.post("/api/v1/sessions").json()["session_id"]
        response = client.put(
            "/api/v1/memory/working",
            json={"expected_version": 1, "current_goal": "session one goal"},
            headers={"X-NanoMemory-Session-ID": first},
        )
        assert response.status_code == 200
        other = client.get("/api/v1/memory/working", headers={"X-NanoMemory-Session-ID": second})
        assert other.json()["document"]["current_goal"] == "尚未設定"


def test_admin_operations_disabled_without_token(api) -> None:
    client, _manager = api
    response = client.post("/api/v1/memory/backup", json={"label": "test"})
    assert response.status_code == 503
    assert response.json()["code"] == "admin_operations_disabled"


def test_admin_token_required_for_destructive_ops(admin_api) -> None:
    client, _manager = admin_api
    missing = client.post("/api/v1/memory/backup", json={"label": "test"})
    assert missing.status_code == 401
    assert missing.json()["code"] == "admin_auth_required"

    wrong = client.post(
        "/api/v1/memory/backup",
        json={"label": "test"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "admin_auth_invalid"

    ok = client.post(
        "/api/v1/memory/backup",
        json={"label": "test"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert ok.status_code == 200
    manifest = ok.json()
    assert manifest["backup_id"]

    verify = client.post(
        f"/api/v1/memory/backups/{manifest['backup_id']}/verify",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True

    bad_confirm = client.post(
        "/api/v1/memory/restore",
        json={"backup_id": manifest["backup_id"], "confirmation": "wrong"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert bad_confirm.status_code == 400
    assert bad_confirm.json()["code"] == "confirmation_required"


def test_permanent_delete_requires_admin(admin_api) -> None:
    client, _manager = admin_api
    created = client.post(
        "/api/v1/memory/long-term",
        json={"content": "delete me permanently", "category": "test"},
    ).json()
    denied = client.delete(
        f"/api/v1/memory/long-term/{created['id']}", params={"permanent": "true"}
    )
    assert denied.status_code == 401
    allowed = client.delete(
        f"/api/v1/memory/long-term/{created['id']}",
        params={"permanent": "true"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["permanent"] is True


def test_maintenance_mode_blocks_memory_api(tmp_path) -> None:
    settings, manager = _manager(tmp_path)
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        from nanomemory.core.memory_models import MaintenanceMarker

        manager.migrations._write_marker(MaintenanceMarker(migration_id=uuid.uuid4().hex))
        blocked = client.get("/api/v1/memory/working")
        assert blocked.status_code == 503
        assert blocked.json()["code"] == "maintenance_mode"
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["maintenance_mode"] is True
        manager.migrations._clear_marker()


def test_rate_limit(tmp_path) -> None:
    settings, manager = _manager(tmp_path, rate_limit_per_minute=2)
    app = create_app(settings, manager=manager, configure_logs=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        limited = client.get("/api/v1/memory/working")
        assert limited.status_code in {200, 429}
        # exhaust the memory bucket
        client.get("/api/v1/memory/working")
        response = client.get("/api/v1/memory/working")
        assert response.status_code == 429
        assert response.json()["code"] == "rate_limit_exceeded"
        assert "retry_after_seconds" in response.json()["details"]


def test_openapi_document_available(api) -> None:
    client, _manager = api
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for expected in (
        "/health",
        "/api/v1/memory/working",
        "/api/v1/memory/long-term",
        "/api/v1/agent/run",
        "/api/v1/metrics/usage",
    ):
        assert expected in paths


def test_statistics_endpoint(api) -> None:
    client, _manager = api
    response = client.get("/api/v1/memory/statistics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["sessions"] >= 1
    assert "capacity" in payload["long_term"]
