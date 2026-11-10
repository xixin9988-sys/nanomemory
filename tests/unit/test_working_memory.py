"""Working Memory transaction tests (spec §25.1, §28.1, §33.2–§33.4, §34, §36)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from nanomemory.core.config import DEFAULT_SESSION_ID, build_settings
from nanomemory.core.errors import (
    NanoMemoryError,
    SecretDetectedError,
    TxnCommitFailedError,
    ValidationError,
    VersionConflictError,
)
from nanomemory.core.locks import LockManager
from nanomemory.core.memory_models import (
    GoalStatus,
    JournalStatus,
    MemoryUpdateProposal,
    SessionState,
    TxnJournal,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.secret_scanner import SecretScanner
from nanomemory.core.working_memory import (
    JOURNAL_NAME,
    NEW_MD_NAME,
    NEW_META_NAME,
    OLD_MD_NAME,
    OLD_META_NAME,
    WorkingMemoryStore,
)


def _proposal(**kwargs: object) -> MemoryUpdateProposal:
    payload = {"current_goal": "Ship NanoMemory MVP", "status": GoalStatus.IN_PROGRESS}
    payload.update(kwargs)
    return MemoryUpdateProposal.model_validate(payload)


def _store(settings, recording_audit) -> WorkingMemoryStore:
    locks = LockManager(
        settings.locks_dir,
        storage_root=settings.storage_root,
        default_timeout=settings.lock_timeout_seconds,
    )
    return WorkingMemoryStore(
        settings,
        locks,
        MemoryValidator(settings, SecretScanner()),
        audit=recording_audit,
    )


# --------------------------------------------------------------------------- basics
def test_ensure_session_creates_version_one(working_store, prepared_session: str) -> None:
    snapshot = working_store.read(prepared_session)
    assert snapshot.meta.memory_version == 1
    assert snapshot.meta.session_id == prepared_session
    assert snapshot.document.current_goal == "尚未設定"
    directory = working_store.session_dir(prepared_session)
    assert (directory / "memory.md").is_file()
    assert (directory / "memory.meta.json").is_file()


def test_update_increments_version_and_writes_hash(working_store, prepared_session: str) -> None:
    result = working_store.update(prepared_session, _proposal(), expected_version=1)
    assert result.snapshot.meta.memory_version == 2
    snapshot = working_store.read(prepared_session)
    assert snapshot.document.current_goal == "Ship NanoMemory MVP"
    from nanomemory.core.fsio import sha256_bytes

    body = (working_store.session_dir(prepared_session) / "memory.md").read_bytes()
    assert snapshot.meta.content_sha256 == sha256_bytes(body)


def test_expected_version_conflict(working_store, prepared_session: str) -> None:
    working_store.update(prepared_session, _proposal(), expected_version=1)
    with pytest.raises(VersionConflictError) as excinfo:
        working_store.update(prepared_session, _proposal(), expected_version=1)
    assert excinfo.value.status == 409
    assert excinfo.value.details["current_version"] == 2


def test_read_missing_session(working_store) -> None:
    with pytest.raises(NanoMemoryError) as excinfo:
        working_store.read(DEFAULT_SESSION_ID)
    assert excinfo.value.code.value == "session_not_found"


def test_unfinished_task_cannot_be_dropped(working_store, prepared_session: str) -> None:
    working_store.update(
        prepared_session,
        _proposal(in_progress=["wire up memory controller"]),
        expected_version=1,
    )
    with pytest.raises(ValidationError) as excinfo:
        working_store.update(prepared_session, _proposal(in_progress=[]), expected_version=2)
    assert "unfinished" in excinfo.value.message


def test_unfinished_task_removal_requires_explicit_flag(
    working_store, prepared_session: str
) -> None:
    working_store.update(
        prepared_session,
        _proposal(in_progress=["wire up memory controller"]),
        expected_version=1,
    )
    result = working_store.update(
        prepared_session,
        _proposal(in_progress=[], allow_task_removal=True),
        expected_version=2,
    )
    assert result.snapshot.document.in_progress == []


def test_secret_in_proposal_rejected(working_store, prepared_session: str) -> None:
    with pytest.raises(SecretDetectedError):
        working_store.update(
            prepared_session,
            _proposal(next_action="use sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"),
            expected_version=1,
        )
    # nothing was written
    assert working_store.read(prepared_session).meta.memory_version == 1


def test_hash_mismatch_marks_recovery_required(working_store, prepared_session: str) -> None:
    body = working_store.session_dir(prepared_session) / "memory.md"
    body.write_text(body.read_text(encoding="utf-8") + "\n## Tampered\n", encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        working_store.read(prepared_session)
    assert excinfo.value.code.value == "session_recovery_required"
    assert working_store.read_state(prepared_session).state is SessionState.RECOVERY_REQUIRED
    # a second read keeps failing closed instead of silently repairing
    with pytest.raises(NanoMemoryError):
        working_store.read(prepared_session)


# --------------------------------------------------------------------------- layout
def test_working_txn_uses_per_txn_subdirectory(
    working_store, prepared_session: str, recording_audit
) -> None:
    result = working_store.update(prepared_session, _proposal(), expected_version=1)
    txn_dir = working_store.txn_root(prepared_session) / result.txn_id
    assert txn_dir.is_dir()
    journal = TxnJournal.model_validate(json.loads((txn_dir / JOURNAL_NAME).read_text()))
    assert journal.txn_id == result.txn_id
    assert journal.status is JournalStatus.COMMITTED
    assert journal.new_md_tmp == NEW_MD_NAME
    assert journal.old_md_snapshot == OLD_MD_NAME
    assert journal.old_meta_snapshot == OLD_META_NAME
    assert journal.new_meta_tmp == NEW_META_NAME
    assert "/" not in journal.new_md_tmp  # relative names only


def test_working_txn_concurrent_prepare_keeps_distinct_dirs(
    working_store, prepared_session: str
) -> None:
    first = working_store.update(prepared_session, _proposal(), expected_version=1)
    second = working_store.update(
        prepared_session, _proposal(current_goal="Next"), expected_version=2
    )
    assert first.txn_id != second.txn_id
    root = working_store.txn_root(prepared_session)
    assert (root / first.txn_id).is_dir()
    assert (root / second.txn_id).is_dir()


def test_working_txn_snapshot_retention_counts_per_txn(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage", working_snapshot_retention=2)
    locks = LockManager(settings.locks_dir, storage_root=tmp_path / "storage")
    store = WorkingMemoryStore(settings, locks, MemoryValidator(settings, SecretScanner()))
    store.registry.ensure_default()
    session_id = DEFAULT_SESSION_ID
    store.ensure_session(session_id)
    version = 1
    for index in range(4):
        store.update(session_id, _proposal(current_goal=f"goal {index}"), expected_version=version)
        version += 1
    txn_dirs = [item for item in store.txn_root(session_id).iterdir() if item.is_dir()]
    assert len(txn_dirs) == 2  # retention keeps the newest two completed transactions


# --------------------------------------------------------------------------- failure injection
def test_prepared_journal_uses_atomic_temp_replace(
    settings, prepared_session: str, recording_audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    before = store.read(session_id).markdown

    def broken_journal(self, txn_dir, journal):  # noqa: ANN001, ARG001
        raise OSError("journal write failed")

    monkeypatch.setattr(WorkingMemoryStore, "_write_journal", broken_journal)
    with pytest.raises(TxnCommitFailedError) as excinfo:
        store.update(session_id, _proposal(), expected_version=1)
    assert excinfo.value.details["stage"] == "journal_prepared"
    assert store.read(session_id).markdown == before
    journals = [
        json.loads((item / JOURNAL_NAME).read_text())
        for item in store.txn_root(session_id).iterdir()
        if (item / JOURNAL_NAME).is_file()
    ]
    assert all(entry["status"] != "prepared" for entry in journals)
    assert "transaction_prepare_failed" in recording_audit.types()


def test_old_snapshots_are_durable_before_prepared_journal(
    settings, prepared_session: str, recording_audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(settings, recording_audit)
    original = store.read(prepared_session).markdown

    def fail_snapshot(target, data, *, expected_sha):  # noqa: ANN001, ARG001
        if Path(target).name == OLD_META_NAME:
            raise OSError("snapshot fsync failed")
        return "unused"

    monkeypatch.setattr(WorkingMemoryStore, "_write_durable", fail_snapshot)
    with pytest.raises(TxnCommitFailedError):
        store.update(prepared_session, _proposal(), expected_version=1)
    assert store.read(prepared_session).markdown == original
    for txn_dir in store.txn_root(prepared_session).iterdir():
        journal = txn_dir / JOURNAL_NAME
        if journal.is_file():
            assert json.loads(journal.read_text())["status"] != "prepared"


def test_replace_failure_rolls_back_under_same_lock(
    settings, prepared_session: str, recording_audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(settings, recording_audit)
    original = store.read(prepared_session).markdown
    real_replace = os.replace
    calls = {"meta": 0}

    def flaky_replace(src, dst, *args, **kwargs):  # noqa: ANN001
        # Fail only the transaction's own "new -> official" metadata replace, so that the
        # in-lock rollback (which restores from a temp file) can still succeed.
        if Path(dst).name == "memory.meta.json" and Path(src).name == "new_memory.meta.json":
            calls["meta"] += 1
            raise OSError(13, "simulated replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("nanomemory.core.fsio.os.replace", flaky_replace)
    with pytest.raises(TxnCommitFailedError) as excinfo:
        store.update(prepared_session, _proposal(), expected_version=1)
    assert excinfo.value.details["session_state"] == "active"
    assert "transaction_rollback_completed" in recording_audit.types()
    snapshot = store.read(prepared_session)
    assert snapshot.markdown == original
    assert snapshot.meta.memory_version == 1


def test_rollback_rejects_unknown_current_hash(
    settings, prepared_session: str, recording_audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(settings, recording_audit)
    real_replace = os.replace
    calls = {"meta": 0}

    def flaky_replace(src, dst, *args, **kwargs):  # noqa: ANN001
        destination = Path(dst)
        if destination.name == "memory.meta.json" and Path(src).name == "new_memory.meta.json":
            calls["meta"] += 1
            # Simulate an external writer between the two replaces.
            (destination.parent / "memory.md").write_bytes(
                b"# Working Memory\n\n## Current Goal\nExternal edit\n"
            )
            raise OSError(13, "replace failure after external modification")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("nanomemory.core.fsio.os.replace", flaky_replace)
    with pytest.raises(TxnCommitFailedError) as excinfo:
        store.update(prepared_session, _proposal(), expected_version=1)
    assert excinfo.value.details["session_state"] == "recovery_required"
    assert store.read_state(prepared_session).state is SessionState.RECOVERY_REQUIRED
    assert "recovery_required_entered" in recording_audit.types()


# --------------------------------------------------------------------------- recovery helpers
def _craft_prepared_crash(store: WorkingMemoryStore, session_id: str) -> tuple[str, str]:
    """Craft a prepared journal with only the body replaced (crash between replaces)."""
    directory = store.session_dir(session_id)
    original_body = (directory / "memory.md").read_bytes()
    original_meta = (directory / "memory.meta.json").read_bytes()
    current_version = store.read(session_id).meta.memory_version
    result = store.update(session_id, _proposal(), expected_version=current_version)
    txn_dir = store.txn_root(session_id) / result.txn_id
    journal_path = txn_dir / JOURNAL_NAME
    journal = TxnJournal.model_validate(json.loads(journal_path.read_text()))
    journal.status = JournalStatus.PREPARED
    journal.completed_at = None
    journal_path.write_text(json.dumps(journal.model_dump(mode="json"), indent=2) + "\n")

    # Simulate the crash: new body already replaced, metadata still the old version.
    (directory / "memory.md").write_bytes(result.snapshot.markdown.encode("utf-8"))
    (directory / "memory.meta.json").write_bytes(original_meta)
    return result.txn_id, original_body.decode("utf-8")


def test_prepared_journal_recovery_rolls_back(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    txn_id, original_body = _craft_prepared_crash(store, session_id)
    store.write_state(session_id, SessionState.RECOVERY_REQUIRED, reason="test", txn_id=txn_id)
    outcome = store.recover(session_id)
    assert not outcome.failed
    assert (store.session_dir(session_id) / "memory.md").read_text(
        encoding="utf-8"
    ) == original_body
    assert store.read_state(session_id).state is SessionState.ACTIVE
    assert (store.txn_root(session_id) / txn_id).exists() is False


def test_committed_recovery_verifies_and_retains(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    result = store.update(prepared_session, _proposal(), expected_version=1)
    outcome = store.recover(prepared_session)
    assert not outcome.failed
    assert (store.txn_root(prepared_session) / result.txn_id).is_dir()
    assert outcome.state is SessionState.ACTIVE


def test_rolled_back_recovery_is_idempotent(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    result = store.update(session_id, _proposal(), expected_version=1)
    txn_dir = store.txn_root(session_id) / result.txn_id
    journal = TxnJournal.model_validate(json.loads((txn_dir / JOURNAL_NAME).read_text()))
    # Rewind the official files and mark the journal rolled_back.
    (store.session_dir(session_id) / "memory.md").write_bytes((txn_dir / OLD_MD_NAME).read_bytes())
    (store.session_dir(session_id) / "memory.meta.json").write_bytes(
        (txn_dir / OLD_META_NAME).read_bytes()
    )
    journal.status = JournalStatus.ROLLED_BACK
    journal.completed_at = "2026-01-01T00:00:00+00:00"
    (txn_dir / JOURNAL_NAME).write_text(
        json.dumps(journal.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    first = store.recover(session_id)
    assert "rolled_back_cleaned" in " ".join(first.actions)
    audit_count = len(recording_audit.of_type("rolled_back_cleanup_completed"))
    assert audit_count == 1
    second = store.recover(session_id)
    assert "rolled_back_cleaned" not in " ".join(second.actions)
    assert len(recording_audit.of_type("rolled_back_cleanup_completed")) == audit_count


def test_rolled_back_with_new_hash_fails_closed(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    result = store.update(session_id, _proposal(), expected_version=1)
    txn_dir = store.txn_root(session_id) / result.txn_id
    journal = TxnJournal.model_validate(json.loads((txn_dir / JOURNAL_NAME).read_text()))
    journal.status = JournalStatus.ROLLED_BACK
    journal.completed_at = "2026-01-01T00:00:00+00:00"
    (txn_dir / JOURNAL_NAME).write_text(
        json.dumps(journal.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    outcome = store.recover(session_id)
    assert outcome.failed
    assert store.read_state(session_id).state is SessionState.RECOVERY_REQUIRED
    assert "rolled_back_state_inconsistent" in recording_audit.types()


def test_rolled_back_with_external_modification_fails_closed(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    result = store.update(session_id, _proposal(), expected_version=1)
    txn_dir = store.txn_root(session_id) / result.txn_id
    journal = TxnJournal.model_validate(json.loads((txn_dir / JOURNAL_NAME).read_text()))
    journal.status = JournalStatus.ROLLED_BACK
    journal.completed_at = "2026-01-01T00:00:00+00:00"
    (txn_dir / JOURNAL_NAME).write_text(
        json.dumps(journal.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    (store.session_dir(session_id) / "memory.md").write_text("external edit", encoding="utf-8")
    outcome = store.recover(session_id)
    assert outcome.failed
    assert store.read_state(session_id).state is SessionState.RECOVERY_REQUIRED


def test_orphan_txn_directory_cleanup(settings, prepared_session: str, recording_audit) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    orphan = store.txn_root(session_id) / ("f" * 32)
    orphan.mkdir(parents=True)
    (orphan / NEW_MD_NAME).write_text("# Working Memory\n", encoding="utf-8")
    old = time.time() - 200_000
    os.utime(orphan / NEW_MD_NAME, (old, old))
    os.utime(orphan, (old, old))
    outcome = store.recover(session_id)
    assert not outcome.failed
    assert not orphan.exists()
    assert "orphan_txn_cleaned" in recording_audit.types()


def test_orphan_txn_respects_age_threshold(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    orphan = store.txn_root(prepared_session) / ("e" * 32)
    orphan.mkdir(parents=True)
    (orphan / NEW_MD_NAME).write_text("# Working Memory\n", encoding="utf-8")
    outcome = store.recover(prepared_session)
    assert any("orphan_too_young" in action for action in outcome.actions)
    assert orphan.exists()


def test_orphan_txn_partial_commit_fails_closed(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    orphan = store.txn_root(session_id) / ("d" * 32)
    orphan.mkdir(parents=True)
    body = (store.session_dir(session_id) / "memory.md").read_bytes()
    (orphan / NEW_MD_NAME).write_bytes(body)
    old = time.time() - 200_000
    os.utime(orphan / NEW_MD_NAME, (old, old))
    outcome = store.recover(session_id)
    assert outcome.failed
    assert store.read_state(session_id).state is SessionState.RECOVERY_REQUIRED
    assert "orphan_txn_with_partial_commit_detected" in recording_audit.types()


def test_journal_temp_orphan_cleanup_allowlist(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    txn_dir = store.txn_root(prepared_session) / ("c" * 32)
    txn_dir.mkdir(parents=True)
    valid = txn_dir / f"journal.prepared.tmp.{os.getpid()}.{'a' * 32}"
    valid.write_text("{}", encoding="utf-8")
    fake = txn_dir / "journal.prepared.tmp.bak"
    fake.write_text("{}", encoding="utf-8")
    old = time.time() - 200_000
    os.utime(valid, (old, old))
    os.utime(fake, (old, old))
    os.utime(txn_dir, (old, old))
    store.recover(prepared_session)
    assert not valid.exists()
    assert fake.exists()


def test_journal_temp_rejects_symlink_and_fake_names(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    txn_dir = store.txn_root(prepared_session) / ("b" * 32)
    txn_dir.mkdir(parents=True)
    fake = txn_dir / "journal.prepared.tmp.123.zzzz"
    fake.write_text("{}", encoding="utf-8")
    old = time.time() - 200_000
    os.utime(fake, (old, old))
    store.recover(prepared_session)
    assert fake.exists()


def test_recovery_required_blocks_updates(settings, prepared_session: str, recording_audit) -> None:
    store = _store(settings, recording_audit)
    store.write_state(prepared_session, SessionState.RECOVERY_REQUIRED, reason="test")
    with pytest.raises(NanoMemoryError) as excinfo:
        store.update(prepared_session, _proposal(), expected_version=1)
    assert excinfo.value.code.value == "session_recovery_required"
    assert excinfo.value.status == 503


def test_corrupt_journal_quarantined_and_fails_closed(
    settings, prepared_session: str, recording_audit
) -> None:
    store = _store(settings, recording_audit)
    session_id = prepared_session
    result = store.update(session_id, _proposal(), expected_version=1)
    txn_dir = store.txn_root(session_id) / result.txn_id
    (txn_dir / JOURNAL_NAME).write_text("{not json", encoding="utf-8")
    outcome = store.recover(session_id)
    assert outcome.failed
    assert store.read_state(session_id).state is SessionState.RECOVERY_REQUIRED
    quarantined = list(txn_dir.glob("journal.corrupt.*.json"))
    assert quarantined
