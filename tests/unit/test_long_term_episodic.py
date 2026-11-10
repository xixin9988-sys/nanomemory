"""Long-term and episodic memory tests (spec §4.2, §4.3, §22.4, §22.11, §24.6, §26.1, §29.6)."""

from __future__ import annotations

import json

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.episodic_memory import EpisodicQuery
from nanomemory.core.errors import NanoMemoryError, SecretDetectedError
from nanomemory.core.locks import LockManager
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import MemoryStatus
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.secret_scanner import SecretScanner

FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "content": "The deployment target is Windows 11 with Python 3.12.",
        "category": "project",
        "source": "user",
        "source_type": "user",
        "confidence": "high",
        "importance": 4,
        "tags": ["deployment", "windows"],
        "trust_level": "user_provided",
    }
    payload.update(overrides)
    return payload


def test_add_and_get(long_term_store) -> None:
    record = long_term_store.add(_payload())
    fetched = long_term_store.get(record.id)
    assert fetched.content == record.content
    assert fetched.version == 1
    assert fetched.schema_version == 1


def test_secret_rejected_on_add(long_term_store) -> None:
    with pytest.raises(SecretDetectedError):
        long_term_store.add(_payload(content=f"api key is {FAKE_OPENAI}"))
    assert long_term_store.list_records() == []


def test_update_requires_matching_version(long_term_store) -> None:
    record = long_term_store.add(_payload())
    updated = long_term_store.update(record.id, {"content": "New content"}, if_version=1)
    assert updated.version == 2
    with pytest.raises(NanoMemoryError) as excinfo:
        long_term_store.update(record.id, {"content": "Again"}, if_version=1)
    assert excinfo.value.code.value == "version_conflict"


def test_archive_is_soft_delete(long_term_store) -> None:
    record = long_term_store.add(_payload())
    archived = long_term_store.archive(record.id)
    assert archived.status is MemoryStatus.ARCHIVED
    assert long_term_store.list_records() == []
    assert long_term_store.list_records(include_archived=True)


def test_permanent_delete(long_term_store) -> None:
    record = long_term_store.add(_payload())
    long_term_store.delete(record.id, permanent=True)
    with pytest.raises(NanoMemoryError) as excinfo:
        long_term_store.get(record.id)
    assert excinfo.value.code.value == "memory_not_found"


def test_capacity_limit(tmp_path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage", long_term_capacity_limit=2)
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    validator = MemoryValidator(settings, SecretScanner())
    store = LongTermMemoryStore(settings, locks, validator)
    store.ensure_storage()
    store.add(_payload(content="first fact"))
    store.add(_payload(content="second fact", tags=["other"]))
    with pytest.raises(NanoMemoryError) as excinfo:
        store.add(_payload(content="third fact", tags=["third"]))
    assert excinfo.value.code.value == "memory_capacity_reached"
    assert store.capacity()["capacity_used"] == 2


def test_capacity_metrics(long_term_store) -> None:
    long_term_store.add(_payload())
    capacity = long_term_store.capacity()
    assert capacity["capacity_used"] == 1
    assert capacity["capacity_limit"] == 5000
    assert capacity["capacity_percent"] >= 0


def test_duplicate_content_marked_conflict(long_term_store) -> None:
    long_term_store.add(_payload(tags=["a"]))
    second = long_term_store.add(_payload(tags=["b"]))
    assert second.status is MemoryStatus.CONFLICT
    assert second.related_ids


def test_overlapping_tags_marked_needs_review(long_term_store) -> None:
    long_term_store.add(_payload(tags=["shared"], content="first statement"))
    second = long_term_store.add(
        _payload(tags=["shared"], content="a different statement about something else")
    )
    assert second.status is MemoryStatus.NEEDS_REVIEW
    assert "possibly related" in (second.review_reason or "")


def test_search_and_filter(long_term_store) -> None:
    long_term_store.add(_payload(content="alpha fact", tags=["x"]))
    long_term_store.add(_payload(content="beta fact", category="other", tags=["y"]))
    assert len(long_term_store.list_records(category="project")) == 1
    assert len(long_term_store.list_records(tag="y")) == 1
    assert long_term_store.search("alpha")[0].content == "alpha fact"


def test_corrupt_knowledge_file_not_overwritten(long_term_store) -> None:
    long_term_store.path.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        long_term_store.add(_payload())
    assert excinfo.value.code.value == "corrupt_data"
    assert long_term_store.path.read_text(encoding="utf-8") == "{invalid json"


# --------------------------------------------------------------------------- episodic
def test_append_and_read(episodic_store) -> None:
    episode = episodic_store.append(
        session_id="s1", event_type="task_started", summary="started work"
    )
    result = episodic_store.read(EpisodicQuery(session_id="s1"))
    assert [view.episode.event_id for view in result.events] == [episode.event_id]
    assert result.clean


def test_jsonl_is_append_only(episodic_store) -> None:
    episodic_store.append(session_id="s1", event_type="task_started", summary="one")
    first = episodic_store.events_path.read_bytes()
    episodic_store.append(session_id="s1", event_type="task_completed", summary="two")
    second = episodic_store.events_path.read_bytes()
    assert second.startswith(first)
    assert len(second.splitlines()) == 2


def test_corrupt_line_quarantined_and_original_untouched(episodic_store) -> None:
    episodic_store.append(session_id="s1", event_type="task_started", summary="valid")
    with open(episodic_store.events_path, "a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    before = episodic_store.events_path.read_bytes()
    result = episodic_store.scan()
    assert result.corrupt_lines == 1
    assert result.quarantined
    assert episodic_store.events_path.read_bytes() == before
    quarantine = episodic_store.quarantine_records()
    assert quarantine and quarantine[0].raw_line.strip() == "this is not json"
    assert quarantine[0].source_file == "events.jsonl"


def test_append_refused_until_acknowledged(episodic_store) -> None:
    with open(episodic_store.events_path, "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    episodic_store.scan()
    pending = episodic_store.pending_quarantine()
    assert pending
    with pytest.raises(NanoMemoryError) as excinfo:
        episodic_store.append(session_id="s1", event_type="task_started", summary="blocked")
    assert excinfo.value.code.value == "quarantine_unacknowledged"

    with pytest.raises(NanoMemoryError):
        episodic_store.acknowledge([], reason="because")

    acks = episodic_store.acknowledge(
        [pending[0].quarantine_id], reason="operator reviewed the damaged line"
    )
    assert acks
    episodic_store.append(session_id="s1", event_type="task_started", summary="allowed")
    # acknowledgement does not claim a repair
    assert episodic_store.scan().corrupt_lines == 1


def test_acknowledge_unknown_record(episodic_store) -> None:
    with open(episodic_store.events_path, "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    episodic_store.scan()
    with pytest.raises(NanoMemoryError) as excinfo:
        episodic_store.acknowledge(["qr-unknown"], reason="test reason")
    assert excinfo.value.code.value == "not_found"


def test_correction_applies_to_latest_view(episodic_store) -> None:
    original = episodic_store.append(
        session_id="s1", event_type="decision_made", summary="use JSON storage"
    )
    correction = episodic_store.append(
        session_id="s1",
        event_type="correction",
        summary="use JSONL storage",
        correction_of=original.event_id,
    )
    latest = episodic_store.read(EpisodicQuery(session_id="s1"))
    ids = [view.episode.event_id for view in latest.events]
    assert original.event_id not in ids
    assert correction.event_id in ids
    view = next(view for view in latest.events if view.episode.event_id == correction.event_id)
    assert view.correction_status == "applied"

    full = episodic_store.read(EpisodicQuery(session_id="s1", include_corrected=True))
    by_id = {view.episode.event_id: view for view in full.events}
    assert by_id[original.event_id].corrected is True
    assert by_id[original.event_id].corrected_by == correction.event_id


def test_correction_target_quarantined_is_unresolved(episodic_store) -> None:
    corrupt = {
        "event_id": "evt-quarantined-target",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "session_id": "s1",
        "event_type": "Bad Event Type",
        "summary": "invalid event type",
    }
    with open(episodic_store.events_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(corrupt) + "\n")
    scanned = episodic_store.scan()
    quarantined = {record.event_id for record in scanned.quarantined}
    assert "evt-quarantined-target" in quarantined
    pending = episodic_store.pending_quarantine()
    episodic_store.acknowledge(
        [record.quarantine_id for record in pending], reason="test correction case"
    )
    correction = episodic_store.append(
        session_id="s1",
        event_type="correction",
        summary="correcting a quarantined event",
        correction_of="evt-quarantined-target",
    )
    latest = episodic_store.read(EpisodicQuery(session_id="s1"))
    ids = [view.episode.event_id for view in latest.events]
    assert correction.event_id not in ids
    assert correction.event_id in latest.unresolved_corrections

    full = episodic_store.read(EpisodicQuery(session_id="s1", include_corrected=True))
    view = next(view for view in full.events if view.episode.event_id == correction.event_id)
    assert view.target_status == "quarantined"
    assert view.correction_status == "unresolved"


def test_correction_target_missing_is_unresolved(episodic_store) -> None:
    correction = episodic_store.append(
        session_id="s1",
        event_type="correction",
        summary="correcting a missing event",
        correction_of="evt-does-not-exist",
    )
    latest = episodic_store.read(EpisodicQuery(session_id="s1"))
    assert correction.event_id not in [view.episode.event_id for view in latest.events]
    assert correction.event_id in latest.unresolved_corrections


def test_filter_by_session_and_type(episodic_store) -> None:
    episodic_store.append(session_id="s1", event_type="task_started", summary="a")
    episodic_store.append(session_id="s2", event_type="task_started", summary="b")
    episodic_store.append(session_id="s1", event_type="task_completed", summary="c")
    assert len(episodic_store.read(EpisodicQuery(session_id="s1")).events) == 2
    assert len(episodic_store.read(EpisodicQuery(event_type="task_completed")).events) == 1


def test_statistics(episodic_store) -> None:
    episodic_store.append(session_id="s1", event_type="task_started", summary="a")
    stats = episodic_store.statistics()
    assert stats["events"] == 1
    assert stats["pending_quarantine"] == 0
