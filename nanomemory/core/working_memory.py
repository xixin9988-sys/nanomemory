"""Working Memory store: Markdown body + JSON sidecar + crash-safe transactions.

Guarantees implemented here (spec §25.1, §28.1, §33.2–§33.4, §34.1–§34.3, §36.1–§36.7):

* ``memory.md`` is human readable content only; the authoritative version, session id and
  content hash live in ``memory.meta.json``.  A body/meta mismatch is never "repaired"
  silently — the session becomes ``recovery_required``.
* Every update runs inside one cross-process ``working`` lock: read version -> validate ->
  durable old snapshots -> durable new files -> atomic prepared journal -> replace body ->
  replace meta -> atomic committed journal -> audit.
* Any failure before the journal is durable leaves the official files untouched.
* Any failure while replacing official files triggers an **in-lock rollback** that first
  verifies each official file's hash is old/new/absent (never overwrites external edits).
* A rollback that cannot be verified fails closed: the session is persisted as
  ``recovery_required``, transaction material is retained, and the caller gets
  ``txn_commit_failed`` with ``details.session_state``.
* Startup recovery (``docs/ARCHITECTURE.md``) handles committed/prepared/rolled_back
  journals, orphan transaction directories and orphan temp files.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from nanomemory.core.config import Settings
from nanomemory.core.errors import (
    ErrorCode,
    NanoMemoryError,
    TxnCommitFailedError,
    ValidationError,
    VersionConflictError,
)
from nanomemory.core.fsio import (
    atomic_write_json,
    fsync_dir,
    is_journal_temp_name,
    is_temp_name,
    journal_temp_name,
    read_bytes,
    read_json,
    read_text,
    replace_with_retry,
    sha256_bytes,
    temp_name_for,
)
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import (
    JOURNAL_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    WORKING_META_SCHEMA_VERSION,
    JournalStatus,
    MemoryUpdateProposal,
    SessionState,
    SessionStateRecord,
    TxnJournal,
    WorkingMemoryDocument,
    WorkingMemoryMeta,
    WorkingMemorySnapshot,
    parse_working_markdown,
    render_working_markdown,
    utc_now_iso,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.paths import ensure_dir, is_link_like
from nanomemory.core.sessions import SessionRegistry

AuditFn = Callable[..., None]

#: Internal file names inside ``working/<session>/.txn/<txn_id>/``.
JOURNAL_NAME = "journal.json"
NEW_MD_NAME = "new_memory.md"
NEW_META_NAME = "new_memory.meta.json"
OLD_MD_NAME = "old_memory.md"
OLD_META_NAME = "old_memory.meta.json"
TXN_DIR_NAME = ".txn"
STATE_NAME = ".state.json"


@dataclass(slots=True)
class UpdateResult:
    """Result of a committed Working Memory update."""

    snapshot: WorkingMemorySnapshot
    txn_id: str
    secret_hits: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecoveryOutcome:
    """What startup recovery did for one session."""

    session_id: str
    actions: list[str] = field(default_factory=list)
    state: SessionState = SessionState.ACTIVE
    failed: bool = False

    def add(self, action: str) -> None:
        self.actions.append(action)


class WorkingMemoryStore:
    """Crash-safe Working Memory storage for one or more sessions."""

    def __init__(
        self,
        settings: Settings,
        locks: LockManager,
        validator: MemoryValidator,
        *,
        audit: AuditFn | None = None,
    ) -> None:
        self.settings = settings
        self.locks = locks
        self.validator = validator
        self.registry = SessionRegistry(settings)
        self._audit_fn = audit
        self.log = get_logger("working_memory")

    # ------------------------------------------------------------------ audit
    def _audit(self, event_type: str, *, session_id: str, **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id=session_id, **fields)
        except Exception:  # noqa: BLE001 - auditing must not break memory correctness
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    # ------------------------------------------------------------------ paths
    def session_dir(self, session_id: str, *, create: bool = False) -> Path:
        return self.registry.session_dir(session_id, create=create)

    def body_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "memory.md"

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "memory.meta.json"

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / STATE_NAME

    def txn_root(self, session_id: str) -> Path:
        return self.session_dir(session_id) / TXN_DIR_NAME

    # ------------------------------------------------------------------ session state
    def read_state(self, session_id: str) -> SessionStateRecord:
        path = self.state_path(session_id)
        if not path.is_file():
            return SessionStateRecord(session_id=session_id, state=SessionState.ACTIVE)
        try:
            return SessionStateRecord.model_validate(read_json(path))
        except NanoMemoryError:
            return SessionStateRecord(
                session_id=session_id,
                state=SessionState.RECOVERY_REQUIRED,
                reason="state_file_corrupt",
            )

    def write_state(
        self,
        session_id: str,
        state: SessionState,
        *,
        reason: str | None = None,
        txn_id: str | None = None,
    ) -> None:
        record = SessionStateRecord(
            session_id=session_id,
            state=state,
            reason=reason,
            txn_id=txn_id,
            updated_at=utc_now_iso(),
        )
        directory = self.session_dir(session_id, create=True)
        atomic_write_json(directory / STATE_NAME, record.model_dump(mode="json"))

    def _guard_active(self, session_id: str) -> None:
        record = self.read_state(session_id)
        if record.state is SessionState.ACTIVE:
            return
        raise NanoMemoryError(
            code=ErrorCode.SESSION_RECOVERY_REQUIRED,
            message="Session requires recovery; read/write is disabled until it completes.",
            details={"session_state": record.state.value, "reason": record.reason},
        )

    def _mark_recovery_required(self, session_id: str, *, reason: str, txn_id: str) -> None:
        try:
            self.write_state(
                session_id, SessionState.RECOVERY_REQUIRED, reason=reason, txn_id=txn_id
            )
        except Exception:  # noqa: BLE001 - fail closed even if the state file cannot be written
            self.log.error(
                "recovery_state_write_failed",
                extra={"event": "recovery_state_write_failed", "txn_id": txn_id},
            )
        self._audit(
            "recovery_required_entered",
            session_id=session_id,
            txn_id=txn_id,
            reason=reason,
        )

    # ------------------------------------------------------------------ read
    def ensure_session(self, session_id: str) -> WorkingMemorySnapshot:
        """Create the initial version (1) of a session's Working Memory if absent."""
        self.registry.validate_id(session_id)
        with self.locks.acquire(["working", f"working:{session_id}"], operation="ensure_session"):
            return self._ensure_session_locked(session_id)

    def _ensure_session_locked(self, session_id: str) -> WorkingMemorySnapshot:
        directory = self.session_dir(session_id, create=True)
        body = directory / "memory.md"
        meta = directory / "memory.meta.json"
        if body.exists() and meta.exists():
            return self._read_snapshot_locked(session_id)
        if body.exists() != meta.exists():
            self._mark_recovery_required(session_id, reason="body_meta_missing_peer", txn_id="none")
            raise NanoMemoryError(
                code=ErrorCode.SESSION_RECOVERY_REQUIRED,
                message="Working memory body and metadata are inconsistent.",
                details={"session_state": SessionState.RECOVERY_REQUIRED.value},
            )
        document = WorkingMemoryDocument()
        result = self._commit(
            session_id,
            None,
            document,
            new_version=1,
            secret_hits=[],
            operation="ensure_session",
        )
        return result.snapshot

    def read(self, session_id: str) -> WorkingMemorySnapshot:
        """Read Working Memory under the working lock, verifying body/meta consistency."""
        self.registry.require(session_id)
        with self.locks.acquire(["working", f"working:{session_id}"], operation="read_working"):
            return self._read_snapshot_locked(session_id)

    def _read_snapshot_locked(self, session_id: str) -> WorkingMemorySnapshot:
        directory = self.session_dir(session_id)
        body = directory / "memory.md"
        meta_path = directory / "memory.meta.json"
        self._guard_active(session_id)
        if not body.exists() or not meta_path.exists():
            if not directory.is_dir():
                raise NanoMemoryError(
                    code=ErrorCode.SESSION_NOT_FOUND, message="Session does not exist."
                )
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="Working memory files are missing.",
                details={"body": body.exists(), "meta": meta_path.exists()},
            )
        meta = self._load_meta(meta_path)
        raw = read_bytes(body)
        actual = sha256_bytes(raw)
        if actual != meta.content_sha256:
            self._mark_recovery_required(session_id, reason="content_hash_mismatch", txn_id="none")
            raise NanoMemoryError(
                code=ErrorCode.SESSION_RECOVERY_REQUIRED,
                message="Working memory content does not match its metadata hash.",
                details={"session_state": SessionState.RECOVERY_REQUIRED.value},
            )
        try:
            document = parse_working_markdown(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._mark_recovery_required(session_id, reason="markdown_parse_failed", txn_id="none")
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="Working memory markdown cannot be parsed.",
                details={"reason": str(exc)[:200]},
            ) from exc
        return WorkingMemorySnapshot(
            session_id=session_id,
            document=document,
            meta=meta,
            markdown=raw.decode("utf-8"),
        )

    def _load_meta(self, path: Path) -> WorkingMemoryMeta:
        try:
            payload = read_json(path)
            meta = WorkingMemoryMeta.model_validate(payload)
        except NanoMemoryError:
            raise
        except Exception as exc:
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="Working memory metadata is invalid.",
                details={"reason": str(exc).splitlines()[0][:200]},
            ) from exc
        supported = SUPPORTED_SCHEMA_VERSIONS["working_meta"]
        if meta.schema_version > supported:
            raise NanoMemoryError(
                code=ErrorCode.SCHEMA_UNSUPPORTED,
                message="Working memory metadata schema is newer than this build supports.",
                details={"found": meta.schema_version, "supported": supported},
            )
        return meta

    # ------------------------------------------------------------------ update
    def update(
        self,
        session_id: str,
        proposal: MemoryUpdateProposal,
        *,
        expected_version: int | None = None,
        operation: str = "update_working",
    ) -> UpdateResult:
        """Validate and apply a proposal inside one lock-held transaction."""
        self.registry.require(session_id)
        with self.locks.acquire(["working", f"working:{session_id}"], operation=operation):
            self._guard_active(session_id)
            snapshot = self._read_snapshot_locked(session_id)
            current_version = snapshot.meta.memory_version
            if expected_version is not None and expected_version != current_version:
                raise VersionConflictError(expected_version, current_version)
            outcome = self.validator.validate_working_update(proposal, snapshot.document)
            return self._commit(
                session_id,
                snapshot,
                outcome.value,
                new_version=current_version + 1,
                secret_hits=outcome.secret_hits,
                operation=operation,
            )

    # ------------------------------------------------------------------ commit
    def _commit(
        self,
        session_id: str,
        old_snapshot: WorkingMemorySnapshot | None,
        document: WorkingMemoryDocument,
        *,
        new_version: int,
        secret_hits: list[str],
        operation: str,
    ) -> UpdateResult:
        directory = self.session_dir(session_id, create=True)
        body_path = directory / "memory.md"
        meta_path = directory / "memory.meta.json"

        if body_path.exists() != meta_path.exists():
            self._mark_recovery_required(session_id, reason="body_meta_missing_peer", txn_id="none")
            raise TxnCommitFailedError(
                "Working memory body and metadata are inconsistent.",
                session_state=SessionState.RECOVERY_REQUIRED.value,
            )

        markdown = render_working_markdown(document)
        body_bytes = markdown.encode("utf-8")
        body_sha = sha256_bytes(body_bytes)
        meta = WorkingMemoryMeta(
            schema_version=WORKING_META_SCHEMA_VERSION,
            memory_version=new_version,
            session_id=session_id,
            updated_at=utc_now_iso(),
            content_sha256=body_sha,
            status=SessionState.ACTIVE,
        )
        meta_bytes = (json.dumps(meta.model_dump(mode="json"), indent=2) + "\n").encode("utf-8")
        meta_sha = sha256_bytes(meta_bytes)

        old_exists = bool(old_snapshot is not None and body_path.exists() and meta_path.exists())
        if old_exists and old_snapshot is not None:
            if old_snapshot.meta.memory_version != new_version - 1:
                raise VersionConflictError(new_version - 1, old_snapshot.meta.memory_version)

        txn_id = uuid.uuid4().hex
        txn_dir = ensure_dir(
            self.txn_root(session_id) / txn_id,
            root=self.settings.storage_root,
            label="transaction directory",
        )
        journal = TxnJournal(
            journal_schema_version=JOURNAL_SCHEMA_VERSION,
            txn_id=txn_id,
            session_id=session_id,
            old_exists=old_exists,
            old_md_sha256=old_snapshot.meta.content_sha256 if old_exists and old_snapshot else None,
            old_meta_sha256=self._hash_existing(meta_path) if old_exists else None,
            new_md_sha256=body_sha,
            new_meta_sha256=meta_sha,
        )

        # ---- 1/2. durable old snapshots, then durable new files -----------------
        try:
            if old_exists:
                self._write_durable(
                    txn_dir / OLD_MD_NAME,
                    read_bytes(body_path),
                    expected_sha=journal.old_md_sha256,
                )
                self._write_durable(
                    txn_dir / OLD_META_NAME,
                    read_bytes(meta_path),
                    expected_sha=journal.old_meta_sha256,
                )
            self._write_durable(txn_dir / NEW_MD_NAME, body_bytes, expected_sha=body_sha)
            self._write_durable(txn_dir / NEW_META_NAME, meta_bytes, expected_sha=meta_sha)
            self._verify_new_files(txn_dir, session_id, new_version, body_sha, meta_sha)
        except BaseException as exc:
            self._discard_txn_dir(txn_dir)
            self._audit(
                "transaction_prepare_failed",
                session_id=session_id,
                txn_id=txn_id,
                operation=operation,
                reason=type(exc).__name__,
            )
            raise TxnCommitFailedError(
                "Working memory update failed during prepare; official files unchanged.",
                session_state=SessionState.ACTIVE.value,
                details={"txn_id": txn_id, "stage": "prepare"},
            ) from exc

        # ---- 3. atomic prepared journal ----------------------------------------
        try:
            self._write_journal(txn_dir, journal)
        except BaseException as exc:
            # Nothing was applied yet; abort the preparation completely.
            self._abort_prepared_before_replace(session_id, txn_dir, journal, operation=operation)
            raise TxnCommitFailedError(
                "Working memory update failed while writing the prepared journal.",
                session_state=SessionState.ACTIVE.value,
                details={"txn_id": txn_id, "stage": "journal_prepared"},
            ) from exc

        # ---- 4. replace official files ----------------------------------------
        try:
            replace_with_retry(txn_dir / NEW_MD_NAME, body_path)
            replace_with_retry(txn_dir / NEW_META_NAME, meta_path)
            fsync_dir(directory)
        except OSError as exc:
            self._rollback_after_replace_failure(
                session_id, txn_dir, journal, reason=f"replace_failed:{exc.errno}"
            )

        # ---- 5. committed journal ---------------------------------------------
        journal.status = JournalStatus.COMMITTED
        journal.completed_at = utc_now_iso()
        try:
            self._write_journal(txn_dir, journal)
        except BaseException:
            self._rollback_after_replace_failure(
                session_id, txn_dir, journal, reason="journal_commit_failed"
            )

        self._audit(
            "memory_updated",
            session_id=session_id,
            txn_id=txn_id,
            version=new_version,
            operation=operation,
            secret_hits=secret_hits,
        )
        snapshot = WorkingMemorySnapshot(
            session_id=session_id, document=document, meta=meta, markdown=markdown
        )
        self._apply_retention(session_id)
        return UpdateResult(snapshot=snapshot, txn_id=txn_id, secret_hits=secret_hits)

    # ------------------------------------------------------------------ durable helpers
    def _hash_existing(self, path: Path) -> str | None:
        if not path.is_file():
            return None
        return sha256_bytes(read_bytes(path))

    def _write_durable(self, target: Path, data: bytes, *, expected_sha: str | None) -> str:
        """Write ``data`` to ``target`` durably and verify its hash before returning."""
        target = Path(target)
        temp = target.parent / temp_name_for(target)
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            actual = sha256_bytes(read_bytes(temp))
            if expected_sha is not None and actual != expected_sha:
                raise NanoMemoryError(
                    code=ErrorCode.IO_ERROR,
                    message="Durable write verification failed (hash mismatch).",
                )
            replace_with_retry(temp, target)
            fsync_dir(target.parent)
            return actual
        except BaseException:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:  # pragma: no cover - best effort
                pass
            raise

    def _verify_new_files(
        self,
        txn_dir: Path,
        session_id: str,
        new_version: int,
        body_sha: str,
        meta_sha: str,
    ) -> None:
        body_bytes = read_bytes(txn_dir / NEW_MD_NAME)
        meta_bytes = read_bytes(txn_dir / NEW_META_NAME)
        if sha256_bytes(body_bytes) != body_sha or sha256_bytes(meta_bytes) != meta_sha:
            raise ValidationError("new transaction files failed hash verification")
        meta = WorkingMemoryMeta.model_validate(json.loads(meta_bytes.decode("utf-8")))
        if meta.session_id != session_id:
            raise ValidationError("new metadata session_id does not match the session")
        if meta.memory_version != new_version:
            raise ValidationError("new metadata memory_version is inconsistent")
        if meta.content_sha256 != sha256_bytes(body_bytes):
            raise ValidationError("new metadata content hash does not match the body")

    def _write_journal(self, txn_dir: Path, journal: TxnJournal) -> None:
        """Atomically replace ``journal.json`` (temp -> fsync -> validate -> replace)."""
        payload = json.dumps(journal.model_dump(mode="json"), indent=2) + "\n"
        temp = txn_dir / journal_temp_name()
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            parsed = TxnJournal.model_validate(json.loads(read_text(temp)))
            if (
                parsed.txn_id != journal.txn_id
                or parsed.session_id != journal.session_id
                or parsed.status is not journal.status
            ):
                raise ValidationError("journal validation mismatch")
            replace_with_retry(temp, txn_dir / JOURNAL_NAME)
            fsync_dir(txn_dir)
        except BaseException:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:  # pragma: no cover - best effort
                pass
            raise

    # ------------------------------------------------------------------ failure paths
    def _discard_txn_dir(self, txn_dir: Path) -> None:
        """Remove a transaction directory that was never made durable (prepare abort)."""
        try:
            for child in sorted(txn_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if child.is_file() and not is_link_like(child):
                    child.unlink()
            if txn_dir.is_dir():
                txn_dir.rmdir()
        except OSError:  # pragma: no cover - best effort; recovery will clean up later
            self.log.warning("txn_dir_discard_failed", extra={"event": "txn_dir_discard_failed"})

    def _abort_prepared_before_replace(
        self,
        session_id: str,
        txn_dir: Path,
        journal: TxnJournal,
        *,
        operation: str,
    ) -> None:
        """Preparation failure after the journal may exist: official files are untouched."""
        directory = txn_dir.parent.parent
        body_path = directory / "memory.md"
        meta_path = directory / "memory.meta.json"
        current_body = self._hash_existing(body_path)
        current_meta = self._hash_existing(meta_path)
        expected_body = journal.old_md_sha256 if journal.old_exists else None
        expected_meta = journal.old_meta_sha256 if journal.old_exists else None
        if current_body != expected_body or current_meta != expected_meta:
            self._mark_recovery_required(
                session_id, reason="prepare_abort_hash_mismatch", txn_id=journal.txn_id
            )
            return
        self._discard_txn_dir(txn_dir)
        self._audit(
            "transaction_prepare_failed",
            session_id=session_id,
            txn_id=journal.txn_id,
            operation=operation,
            reason="journal_write_failed",
        )

    def _rollback_after_replace_failure(
        self,
        session_id: str,
        txn_dir: Path,
        journal: TxnJournal,
        *,
        reason: str,
    ) -> NoReturn:
        """Verify current hashes, restore the old state in-lock, then fail the request."""
        directory = txn_dir.parent.parent
        body_path = directory / "memory.md"
        meta_path = directory / "memory.meta.json"
        allowed_body = {journal.new_md_sha256}
        allowed_meta = {journal.new_meta_sha256}
        if journal.old_exists:
            allowed_body.add(journal.old_md_sha256 or "")
            allowed_meta.add(journal.old_meta_sha256 or "")
        else:
            allowed_body.add(None)  # type: ignore[arg-type]
            allowed_meta.add(None)  # type: ignore[arg-type]

        current_body = self._hash_existing(body_path)
        current_meta = self._hash_existing(meta_path)
        if current_body not in allowed_body or current_meta not in allowed_meta:
            self._mark_recovery_required(
                session_id, reason="rollback_unknown_current_hash", txn_id=journal.txn_id
            )
            self._audit(
                "recovery_external_modification_detected",
                session_id=session_id,
                txn_id=journal.txn_id,
            )
            raise TxnCommitFailedError(
                "Rollback aborted: an official file changed outside this transaction.",
                session_state=SessionState.RECOVERY_REQUIRED.value,
                details={"txn_id": journal.txn_id, "stage": "rollback_verify"},
            )

        try:
            self._restore_old_state(
                directory,
                txn_dir,
                journal,
                current_body=current_body,
                current_meta=current_meta,
            )
            journal.status = JournalStatus.ROLLED_BACK
            journal.completed_at = utc_now_iso()
            self._write_journal(txn_dir, journal)
        except BaseException as exc:
            self._mark_recovery_required(
                session_id, reason=f"rollback_failed:{reason}", txn_id=journal.txn_id
            )
            raise TxnCommitFailedError(
                "Rollback failed; session requires recovery.",
                session_state=SessionState.RECOVERY_REQUIRED.value,
                details={"txn_id": journal.txn_id, "stage": "rollback"},
            ) from exc

        self._audit(
            "transaction_rollback_completed",
            session_id=session_id,
            txn_id=journal.txn_id,
            reason=reason,
        )
        self._discard_txn_dir(txn_dir)
        raise TxnCommitFailedError(
            "Working memory transaction was rolled back and reported as failed.",
            session_state=SessionState.ACTIVE.value,
            details={"txn_id": journal.txn_id, "reason": reason},
        )

    def _restore_old_state(
        self,
        directory: Path,
        txn_dir: Path,
        journal: TxnJournal,
        *,
        current_body: str | None,
        current_meta: str | None,
    ) -> None:
        """Restore old snapshots where the official file currently holds the new content."""
        body_path = directory / "memory.md"
        meta_path = directory / "memory.meta.json"
        if journal.old_exists:
            if current_body != journal.old_md_sha256:
                self._write_durable(
                    body_path,
                    read_bytes(txn_dir / OLD_MD_NAME),
                    expected_sha=journal.old_md_sha256,
                )
            if current_meta != journal.old_meta_sha256:
                self._write_durable(
                    meta_path,
                    read_bytes(txn_dir / OLD_META_NAME),
                    expected_sha=journal.old_meta_sha256,
                )
        else:
            if current_body == journal.new_md_sha256:
                body_path.unlink()
            if current_meta == journal.new_meta_sha256:
                meta_path.unlink()
        fsync_dir(directory)
        self._verify_official_state(directory, journal)

    def _verify_official_state(self, directory: Path, journal: TxnJournal) -> None:
        body = self._hash_existing(directory / "memory.md")
        meta = self._hash_existing(directory / "memory.meta.json")
        if journal.old_exists:
            if body != journal.old_md_sha256 or meta != journal.old_meta_sha256:
                raise ValidationError("official files do not match the old snapshot hashes")
        elif body is not None or meta is not None:
            raise ValidationError("first-creation transaction left official files behind")

    # ------------------------------------------------------------------ recovery
    def recover(self, session_id: str) -> RecoveryOutcome:
        """Run recovery for one session under the working lock (idempotent)."""
        if not self.registry.exists(session_id):
            return RecoveryOutcome(session_id=session_id, actions=["missing_session"])
        with self.locks.acquire(["working", f"working:{session_id}"], operation="recovery"):
            return self._recover_locked(session_id)

    def _recover_locked(self, session_id: str) -> RecoveryOutcome:
        outcome = RecoveryOutcome(session_id=session_id)
        directory = self.session_dir(session_id)
        if not directory.is_dir():
            outcome.add("missing_session")
            return outcome
        state_before = self.read_state(session_id)
        outcome.state = state_before.state

        txn_root = self.txn_root(session_id)
        if txn_root.is_dir():
            # Journal temp cleanup runs first: a directory holding only a journal temp is
            # otherwise removed wholesale by the orphan-transaction rule, which would hide
            # the temp-specific allowlist semantics of spec §36.4.
            self._cleanup_journal_temps(txn_root, outcome)
            entries = sorted(
                (
                    entry
                    for entry in txn_root.iterdir()
                    if entry.is_dir() and not is_link_like(entry)
                ),
                key=lambda item: item.name,
            )
            parsed: dict[Path, TxnJournal | None] = {}
            for txn_dir in entries:
                if not (txn_dir / JOURNAL_NAME).is_file():
                    continue
                try:
                    parsed[txn_dir] = self._load_journal(txn_dir)
                except Exception:  # noqa: BLE001 - corrupt journal handled below
                    parsed[txn_dir] = None
            latest_dir = None
            newest: list[tuple[str, Path]] = []
            for txn_dir, journal in parsed.items():
                if journal is None:
                    continue
                stamp = journal.completed_at or journal.started_at
                if journal.status is JournalStatus.PREPARED:
                    stamp = journal.started_at
                newest.append((stamp, txn_dir))
            if newest:
                latest_dir = max(newest, key=lambda item: item[0])[1]
            for txn_dir in entries:
                if txn_dir in parsed:
                    self._recover_with_journal(
                        session_id,
                        directory,
                        txn_dir,
                        parsed[txn_dir],
                        outcome,
                        is_latest_committed=txn_dir == latest_dir,
                    )
                else:
                    self._recover_orphan_txn(session_id, directory, txn_dir, outcome)
        self._cleanup_general_temps(directory, outcome)

        final_state = self.read_state(session_id).state
        if not outcome.failed and state_before.state in {
            SessionState.RECOVERY_REQUIRED,
            SessionState.RECOVERY_PENDING,
        }:
            self.write_state(session_id, SessionState.ACTIVE)
            self._audit("recovery_completed", session_id=session_id)
            final_state = SessionState.ACTIVE
        outcome.state = final_state
        return outcome

    def _load_journal(self, txn_dir: Path) -> TxnJournal:
        payload = read_json(txn_dir / JOURNAL_NAME)
        journal = TxnJournal.model_validate(payload)
        if journal.journal_schema_version > SUPPORTED_SCHEMA_VERSIONS["journal"]:
            raise NanoMemoryError(
                code=ErrorCode.SCHEMA_UNSUPPORTED,
                message="Transaction journal schema is newer than this build supports.",
            )
        return journal

    def _recover_with_journal(
        self,
        session_id: str,
        directory: Path,
        txn_dir: Path,
        journal: TxnJournal | None,
        outcome: RecoveryOutcome,
        *,
        is_latest_committed: bool,
    ) -> None:
        if journal is None:
            self._quarantine_journal(txn_dir, outcome)
            self._mark_recovery_required(session_id, reason="journal_corrupt", txn_id=txn_dir.name)
            outcome.failed = True
            outcome.add(f"journal_corrupt:{txn_dir.name}")
            return

        if journal.status is JournalStatus.COMMITTED:
            self._recover_committed(
                session_id, directory, txn_dir, journal, outcome, is_latest=is_latest_committed
            )
        elif journal.status is JournalStatus.PREPARED:
            self._recover_prepared(session_id, directory, txn_dir, journal, outcome)
        else:
            self._recover_rolled_back(session_id, directory, txn_dir, journal, outcome)

    def _recover_committed(
        self,
        session_id: str,
        directory: Path,
        txn_dir: Path,
        journal: TxnJournal,
        outcome: RecoveryOutcome,
        *,
        is_latest: bool,
    ) -> None:
        if not is_latest:
            # Historical committed transactions are retained snapshots: later transactions
            # legitimately replaced the official files, so their hashes cannot match anymore.
            return
        body = self._hash_existing(directory / "memory.md")
        meta = self._hash_existing(directory / "memory.meta.json")
        if body == journal.new_md_sha256 and meta == journal.new_meta_sha256:
            # Verified and retained by the snapshot retention policy; nothing to do.
            return
        self._mark_recovery_required(
            session_id, reason="committed_hash_mismatch", txn_id=journal.txn_id
        )
        self._audit(
            "recovery_external_modification_detected",
            session_id=session_id,
            txn_id=journal.txn_id,
        )
        outcome.failed = True
        outcome.add(f"committed_hash_mismatch:{journal.txn_id}")

    def _recover_prepared(
        self,
        session_id: str,
        directory: Path,
        txn_dir: Path,
        journal: TxnJournal,
        outcome: RecoveryOutcome,
    ) -> None:
        body = self._hash_existing(directory / "memory.md")
        meta = self._hash_existing(directory / "memory.meta.json")
        allowed_body = {journal.new_md_sha256}
        allowed_meta = {journal.new_meta_sha256}
        if journal.old_exists:
            allowed_body.add(journal.old_md_sha256 or "")
            allowed_meta.add(journal.old_meta_sha256 or "")
        else:
            allowed_body.add(None)  # type: ignore[arg-type]
            allowed_meta.add(None)  # type: ignore[arg-type]
        if body not in allowed_body or meta not in allowed_meta:
            self._mark_recovery_required(
                session_id, reason="prepared_unknown_hash", txn_id=journal.txn_id
            )
            self._audit(
                "recovery_external_modification_detected",
                session_id=session_id,
                txn_id=journal.txn_id,
            )
            outcome.failed = True
            outcome.add(f"prepared_unknown_hash:{journal.txn_id}")
            return
        try:
            self._restore_old_state(
                directory, txn_dir, journal, current_body=body, current_meta=meta
            )
            journal.status = JournalStatus.ROLLED_BACK
            journal.completed_at = utc_now_iso()
            self._write_journal(txn_dir, journal)
        except Exception:  # noqa: BLE001 - rollback failure fails closed
            self._mark_recovery_required(
                session_id, reason="prepared_rollback_failed", txn_id=journal.txn_id
            )
            outcome.failed = True
            outcome.add(f"prepared_rollback_failed:{journal.txn_id}")
            return
        self._audit(
            "transaction_rollback_completed",
            session_id=session_id,
            txn_id=journal.txn_id,
            reason="startup_recovery",
        )
        self._discard_txn_dir(txn_dir)
        outcome.add(f"prepared_rolled_back:{journal.txn_id}")

    def _recover_rolled_back(
        self,
        session_id: str,
        directory: Path,
        txn_dir: Path,
        journal: TxnJournal,
        outcome: RecoveryOutcome,
    ) -> None:
        body = self._hash_existing(directory / "memory.md")
        meta = self._hash_existing(directory / "memory.meta.json")
        if journal.old_exists:
            consistent = body == journal.old_md_sha256 and meta == journal.old_meta_sha256
            saw_new = body == journal.new_md_sha256 or meta == journal.new_meta_sha256
        else:
            consistent = body is None and meta is None
            saw_new = body == journal.new_md_sha256 or meta == journal.new_meta_sha256

        if consistent:
            self._discard_txn_dir(txn_dir)
            self._audit(
                "rolled_back_cleanup_completed",
                session_id=session_id,
                txn_id=journal.txn_id,
            )
            outcome.add(f"rolled_back_cleaned:{journal.txn_id}")
            return
        if saw_new:
            self._mark_recovery_required(
                session_id, reason="rolled_back_new_hash", txn_id=journal.txn_id
            )
            self._audit(
                "rolled_back_state_inconsistent",
                session_id=session_id,
                txn_id=journal.txn_id,
            )
            outcome.failed = True
            outcome.add(f"rolled_back_inconsistent:{journal.txn_id}")
            return
        self._mark_recovery_required(
            session_id, reason="rolled_back_external_modification", txn_id=journal.txn_id
        )
        self._audit(
            "recovery_external_modification_detected",
            session_id=session_id,
            txn_id=journal.txn_id,
        )
        outcome.failed = True
        outcome.add(f"rolled_back_external_modification:{journal.txn_id}")

    def _recover_orphan_txn(
        self,
        session_id: str,
        directory: Path,
        txn_dir: Path,
        outcome: RecoveryOutcome,
    ) -> None:
        files = [
            child for child in txn_dir.iterdir() if child.is_file() and not is_link_like(child)
        ]
        artifacts = [child for child in files if self._is_txn_artifact(child.name)]
        if not artifacts:
            # Not a NanoMemory transaction directory (unknown or user-created files):
            # never delete it, only record a diagnostic (spec §36.5 conditions a-c).
            outcome.add(f"orphan_unrecognised_skipped:{txn_dir.name}")
            return
        if not files:
            if self._age_seconds(txn_dir) > self.settings.orphan_tmp_max_age_seconds:
                self._discard_txn_dir(txn_dir)
                outcome.add(f"orphan_empty_dir_removed:{txn_dir.name}")
            return

        new_body = txn_dir / NEW_MD_NAME
        new_meta = txn_dir / NEW_META_NAME
        official_body = self._hash_existing(directory / "memory.md")
        official_meta = self._hash_existing(directory / "memory.meta.json")
        partial = False
        if new_body.is_file() and sha256_bytes(read_bytes(new_body)) == official_body:
            partial = True
        if new_meta.is_file() and sha256_bytes(read_bytes(new_meta)) == official_meta:
            partial = True
        if partial:
            self._mark_recovery_required(
                session_id, reason="orphan_txn_partial_commit", txn_id=txn_dir.name
            )
            self._audit(
                "orphan_txn_with_partial_commit_detected",
                session_id=session_id,
                txn_id=txn_dir.name,
            )
            outcome.failed = True
            outcome.add(f"orphan_partial_commit:{txn_dir.name}")
            return

        oldest = min(child.stat().st_mtime for child in files)
        age = max(0.0, datetime.now(UTC).timestamp() - oldest)
        if age <= self.settings.orphan_tmp_max_age_seconds:
            outcome.add(f"orphan_too_young:{txn_dir.name}")
            return
        self._audit(
            "orphan_txn_cleaned",
            session_id=session_id,
            txn_id=txn_dir.name,
            age_seconds=round(age, 1),
            reason="orphan_without_journal",
        )
        self._discard_txn_dir(txn_dir)
        outcome.add(f"orphan_cleaned:{txn_dir.name}")

    def _quarantine_journal(self, txn_dir: Path, outcome: RecoveryOutcome) -> None:
        journal_path = txn_dir / JOURNAL_NAME
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = txn_dir / f"journal.corrupt.{stamp}.json"
        try:
            os.replace(journal_path, target)
        except OSError:  # pragma: no cover - keep the journal if it cannot be moved
            outcome.add(f"journal_quarantine_failed:{txn_dir.name}")
        else:
            outcome.add(f"journal_quarantined:{txn_dir.name}")

    # ------------------------------------------------------------------ cleanup
    @staticmethod
    def _is_txn_artifact(name: str) -> bool:
        """True when ``name`` is one of the mandated transaction file names (§28.1/§36.4)."""
        return name in {NEW_MD_NAME, NEW_META_NAME, OLD_MD_NAME, OLD_META_NAME} or (
            is_journal_temp_name(name)
        )

    @staticmethod
    def _age_seconds(path: Path) -> float:
        try:
            return max(0.0, datetime.now(UTC).timestamp() - path.stat().st_mtime)
        except OSError:
            return 0.0

    def _cleanup_journal_temps(self, txn_root: Path, outcome: RecoveryOutcome) -> None:
        """Remove orphan prepared-journal temps in directories without a journal (§36.4)."""
        threshold = self.settings.orphan_tmp_max_age_seconds
        for txn_dir in txn_root.iterdir():
            if not txn_dir.is_dir() or is_link_like(txn_dir):
                continue
            if (txn_dir / JOURNAL_NAME).exists():
                continue  # owned by journal recovery
            for child in txn_dir.iterdir():
                if not child.is_file() or is_link_like(child):
                    continue
                if not is_journal_temp_name(child.name):
                    continue
                if self._age_seconds(child) <= threshold:
                    continue
                try:
                    child.unlink()
                except OSError:  # pragma: no cover
                    continue
                self._audit(
                    "journal_temp_cleaned",
                    session_id="unknown",
                    txn_id=txn_dir.name,
                    file=child.name,
                    reason="orphan_journal_temp",
                )
                outcome.add(f"journal_temp_cleaned:{txn_dir.name}/{child.name}")

    def _cleanup_general_temps(self, directory: Path, outcome: RecoveryOutcome) -> None:
        threshold = self.settings.orphan_tmp_max_age_seconds
        for child in directory.iterdir():
            if not child.is_file() or is_link_like(child):
                continue
            if not is_temp_name(child.name):
                continue
            if self._age_seconds(child) <= threshold:
                continue
            size = child.stat().st_size
            try:
                child.unlink()
            except OSError:  # pragma: no cover
                continue
            self._audit(
                "orphan_temp_cleaned",
                session_id="unknown",
                file=child.name,
                size=size,
                reason="orphan_atomic_write_temp",
            )
            outcome.add(f"temp_cleaned:{child.name}")

    def _apply_retention(self, session_id: str) -> None:
        """Keep the newest N completed transaction directories (spec §36.1.3)."""
        retention = self.settings.working_snapshot_retention
        txn_root = self.txn_root(session_id)
        if not txn_root.is_dir():
            return
        completed: list[tuple[str, Path]] = []
        in_progress: list[Path] = []
        for txn_dir in txn_root.iterdir():
            if not txn_dir.is_dir() or is_link_like(txn_dir):
                continue
            journal_path = txn_dir / JOURNAL_NAME
            if not journal_path.is_file():
                continue
            try:
                journal = self._load_journal(txn_dir)
            except Exception:  # noqa: BLE001 - leave unreadable journals for recovery
                in_progress.append(txn_dir)
                continue
            if journal.status in {JournalStatus.COMMITTED, JournalStatus.ROLLED_BACK}:
                completed.append((journal.completed_at or journal.started_at, txn_dir))
            else:
                in_progress.append(txn_dir)
        completed.sort(key=lambda item: item[0], reverse=True)
        keep = max(0, retention)
        for _stamp, txn_dir in completed[keep:]:
            if any(txn_dir == active for active in in_progress):  # pragma: no cover
                continue
            age = self._age_seconds(txn_dir)
            self._audit(
                "orphan_txn_cleaned",
                session_id=session_id,
                txn_id=txn_dir.name,
                age_seconds=round(age, 1),
                reason="retention_exceeded",
            )
            self._discard_txn_dir(txn_dir)

    # ------------------------------------------------------------------ statistics
    def statistics(self, session_id: str) -> dict[str, Any]:
        record = self.read_state(session_id)
        txn_count = 0
        txn_root = self.txn_root(session_id)
        if txn_root.is_dir():
            txn_count = sum(1 for item in txn_root.iterdir() if item.is_dir())
        body = self.body_path(session_id)
        return {
            "session_id": session_id,
            "state": record.state.value,
            "body_bytes": body.stat().st_size if body.is_file() else 0,
            "transaction_dirs": txn_count,
        }


__all__ = [
    "JOURNAL_NAME",
    "NEW_MD_NAME",
    "NEW_META_NAME",
    "OLD_MD_NAME",
    "OLD_META_NAME",
    "RecoveryOutcome",
    "UpdateResult",
    "WorkingMemoryStore",
]
