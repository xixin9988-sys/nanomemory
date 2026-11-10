"""Cognitive memory model: provenance, supersede, confirmation, lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanomemory.core.config import build_settings
from nanomemory.core.lifecycle import is_protected, plan_archival, run_lifecycle
from nanomemory.core.locks import LockManager
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import (
    MemoryStatus,
    MemoryType,
    Provenance,
    SourceKind,
)
from nanomemory.core.memory_retriever import MIN_FINAL_SCORE, detect_intent, rank_records
from nanomemory.core.memory_validator import MemoryValidator


def _store(tmp_path: Path) -> LongTermMemoryStore:
    settings = build_settings(storage_root=tmp_path / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    return LongTermMemoryStore(settings, locks, MemoryValidator(settings))


def _payload(
    content: str,
    *,
    memory_type: MemoryType = MemoryType.FACT,
    source_kind: SourceKind = SourceKind.USER,
    importance: int = 3,
    user_id: str = "u1",
    created_at: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "content": content,
        "category": "general",
        "memory_type": memory_type.value,
        "importance": importance,
        "source_type": source_kind.value,
        "source": "test",
        "user_id": user_id,
        "provenance": Provenance(
            source_kind=source_kind, source_ref="test", created_by="test"
        ).model_dump(mode="json"),
    }
    if created_at:
        payload["created_at"] = created_at
    return payload


def test_model_derived_memory_is_pending_until_confirmed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.add(_payload("The service uses a queue.", source_kind=SourceKind.MODEL))
    assert record.status is MemoryStatus.PENDING_CONFIRMATION
    assert record.provenance.source_kind is SourceKind.MODEL

    confirmed = store.confirm(record.id)
    assert confirmed.status is MemoryStatus.ACTIVE
    assert confirmed.version == 2


def test_user_update_supersedes_older_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.add(_payload("I use Python for my services.", memory_type=MemoryType.PREFERENCE))
    new = store.add(_payload("I use Rust for my services.", memory_type=MemoryType.PREFERENCE))

    old_after = store.get(old.id)
    assert old_after.status is MemoryStatus.SUPERSEDED
    assert old_after.superseded_by == new.id
    assert old_after.valid_until is not None
    assert new.supersedes == [old.id]

    active = store.list_records(limit=50)
    assert [record.id for record in active] == [new.id]
    history = store.list_records(limit=50, include_superseded=True)
    assert len(history) == 2

    explanation = store.explain_data(old.id)
    assert explanation["superseded_by"] == new.id
    assert explanation["supersede_chain"][0]["id"] == new.id


def test_duplicate_content_marks_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_payload("The API runs on port 8000."))
    duplicate = store.add(_payload("The API runs on port 8000."))
    assert duplicate.status is MemoryStatus.CONFLICT


def test_touch_recalled_increments_utility(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.add(_payload("The dashboard uses a dark theme."))
    assert store.touch_recalled([record.id]) == 1
    updated = store.get(record.id)
    assert updated.recall_count == 1
    assert updated.last_recalled_at is not None


def test_lifecycle_archives_stale_low_value_and_protects_preferences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    long_ago = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    stale = store.add(
        _payload(
            "A temporary note about an old experiment.",
            importance=1,
            created_at=long_ago,
        )
    )
    preference = store.add(
        _payload(
            "I prefer dark mode.",
            memory_type=MemoryType.PREFERENCE,
            importance=1,
            created_at=long_ago,
        )
    )
    assert is_protected(preference)
    decisions = plan_archival([store.get(stale.id), store.get(preference.id)])
    assert [decision["id"] for decision in decisions] == [stale.id]

    dry = run_lifecycle(store, dry_run=True)
    assert dry["eligible"] >= 1
    assert store.get(stale.id).status is MemoryStatus.ACTIVE

    applied = run_lifecycle(store, dry_run=False)
    assert applied["archived"] >= 1
    assert store.get(stale.id).status is MemoryStatus.ARCHIVED
    assert store.get(preference.id).status is MemoryStatus.ACTIVE


def test_intent_detection_and_ranking() -> None:
    assert detect_intent("What language do I prefer?").value == "preference"
    assert detect_intent("Why did we decide to use Rust?").value == "decision_history"
    assert detect_intent("What is the current project status?").value == "project_state"
    assert detect_intent("When did we last deploy?").value == "timeline"
    assert detect_intent("Tell me something").value == "general"


def test_rank_records_prefers_matching_preference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(
        _payload("I prefer Python for scripting.", memory_type=MemoryType.PREFERENCE, importance=4)
    )
    store.add(_payload("The dashboard uses a dark theme.", importance=2))
    records = store.list_records(limit=50)
    ranked = rank_records(records, "which language do I prefer?")
    assert "python" in ranked[0][0].content.lower()
    assert ranked[0][2]["semantic"] >= 0.0


def test_retriever_reports_insufficient_context(tmp_path: Path) -> None:
    from nanomemory.core.memory_manager import MemoryManager

    manager = MemoryManager(build_settings(storage_root=tmp_path / "store"), configure_logs=False)
    manager.initialize_storage()
    manager.startup()
    manager.remember("The dashboard uses a dark theme by default.", session_id="system")
    result = manager.recall(
        "quantum chromodynamics lattice spacing", session_id=manager.settings.default_session_id
    )
    assert result.insufficient_context is True
    assert result.fallback_reason
    assert MIN_FINAL_SCORE > 0
