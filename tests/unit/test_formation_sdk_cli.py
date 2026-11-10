"""Formation pipeline, SDK and CLI end-to-end behaviour."""

from __future__ import annotations

import asyncio
from pathlib import Path

from nanomemory.cli import build_parser, main
from nanomemory.core.config import build_settings
from nanomemory.core.locks import LockManager
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_formation import MemoryFormation, classify, extract_candidates
from nanomemory.core.memory_models import MemoryStatus, MemoryType, SourceKind
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.sdk import Memory


def _store(path: Path) -> LongTermMemoryStore:
    settings = build_settings(storage_root=path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    return LongTermMemoryStore(settings, locks, MemoryValidator(settings))


def test_classify_and_extract_skip_questions() -> None:
    assert classify("I prefer Python for tooling.") is MemoryType.PREFERENCE
    assert classify("We decided to migrate the service to Rust.") is MemoryType.EVENT
    assert classify("Step 1: install the package.") is MemoryType.PROCEDURE
    assert classify("The API uses FastAPI.") is MemoryType.FACT
    assert classify("What language do I prefer?") is None

    candidates = extract_candidates(
        "I prefer Python for tooling. What about Rust? The deployment target is Windows 11.",
        source_kind=SourceKind.USER,
    )
    contents = [candidate["content"] for candidate in candidates]
    assert any("prefer Python" in content for content in contents)
    assert all("?" not in content for content in contents)


def test_formation_writes_pending_for_model_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    formation = MemoryFormation(build_settings(storage_root=tmp_path / "storage"), store)
    report = formation.form("The deployment target is Windows 11.", source_kind=SourceKind.MODEL)
    assert report["extracted"] == 1
    assert report["pending"] == 1
    assert report["created"] == 0
    record = store.get(report["items"][0]["id"])
    assert record.status is MemoryStatus.PENDING_CONFIRMATION


def test_formation_supersedes_on_new_statement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    formation = MemoryFormation(build_settings(storage_root=tmp_path / "storage"), store)
    formation.form("I prefer Python for services.", source_kind=SourceKind.USER)
    report = formation.form("I prefer Rust for services.", source_kind=SourceKind.USER)
    assert report["created"] == 1
    assert report["superseded"] >= 1
    active = store.list_records(limit=10)
    assert any("rust" in record.content.lower() for record in active)
    assert all("python" not in record.content.lower() for record in active)


def test_sdk_round_trip_and_forget(tmp_path: Path) -> None:
    memory = Memory(tmp_path)
    report = asyncio.run(
        memory.remember("I prefer Python. The service uses FastAPI.", user_id="u1")
    )
    assert report["created"] >= 1

    recall = asyncio.run(memory.recall("which framework does the service use?", user_id="u1"))
    assert recall["items"]
    target = next(item for item in recall["items"] if item["kind"] == "long_term")

    explained = asyncio.run(memory.explain(target["id"]))
    assert explained["provenance"]["source_kind"] == "user"
    assert explained["audit_events"]

    forgotten = asyncio.run(memory.forget(target["id"], reason="test"))
    assert forgotten["status"] == "archived"
    assert asyncio.run(memory.status())["by_status"].get("archived", 0) >= 1


def test_cli_init_remember_recall(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    assert parser.parse_args(["init", "--path", str(tmp_path)]).func.__name__ == "cmd_init"
    assert main(["init", "--path", str(tmp_path)]) == 0
    assert "initialised" in capsys.readouterr().out

    assert main(["remember", "I prefer Python for tooling.", "--path", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "created" in out

    assert main(["recall", "which language do I prefer?", "--path", str(tmp_path), "--json"]) == 0
    assert "preference" in capsys.readouterr().out
