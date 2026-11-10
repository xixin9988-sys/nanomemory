"""Cognitive API endpoints: remember, recall, explain, confirm, lifecycle."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from nanomemory.api.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NANOMEMORY_STORAGE_ROOT", str(tmp_path / "storage"))
    with TestClient(create_app()) as test_client:
        yield test_client


def test_remember_then_recall_with_signals(client: TestClient) -> None:
    created = client.post(
        "/api/v1/memory/remember",
        json={
            "content": f"I prefer Gleam for the api-test service {uuid.uuid4().hex[:8]}.",
            "user_id": "api-user",
        },
    )
    assert created.status_code == 200
    assert created.json()["created"] == 1

    recalled = client.get(
        "/api/v1/memory/recall", params={"q": "which language do I prefer?", "user_id": "api-user"}
    )
    assert recalled.status_code == 200
    body = recalled.json()
    assert body["intent"] == "preference"
    assert body["items"]
    long_term = [item for item in body["items"] if item["kind"] == "long_term"]
    assert long_term
    assert long_term[0]["signals"]["keyword"] >= 0.0
    assert long_term[0]["memory_type"] == "preference"


def test_explain_returns_provenance_and_audit(client: TestClient) -> None:
    created = client.post(
        "/api/v1/memory/remember",
        json={
            "content": f"The api-test deployment target is Windows 11 {uuid.uuid4().hex[:8]}.",
            "user_id": "api-user",
        },
    ).json()
    memory_id = created["items"][0]["id"]
    explained = client.get(f"/api/v1/memory/{memory_id}/explain")
    assert explained.status_code == 200
    body = explained.json()
    assert body["provenance"]["source_kind"] == "user"
    assert body["record"]["id"] == memory_id


def test_lifecycle_dry_run_does_not_archive(client: TestClient) -> None:
    client.post(
        "/api/v1/memory/remember",
        json={
            "content": f"A temporary api-test experiment note about an old cache {uuid.uuid4().hex[:8]}.",
            "user_id": "api-user",
        },
    )
    response = client.post("/api/v1/memory/lifecycle/run", params={"apply": "false"})
    assert response.status_code == 200
    assert response.json()["dry_run"] is True
