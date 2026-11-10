"""Long-term Memory store (``knowledge.json``).

Rules from the specification that are enforced here:

* every record has its own ``version``; callers must supply ``if_version`` for updates
  (mismatch -> 409 ``version_conflict``),
* ``confidence`` / ``importance`` are clamped by source type, never trusted from the model
  (spec §22.11),
* capacity is capped (default 5000 active records) and exceeding it is an explicit
  ``memory_capacity_reached`` error, never a silent drop (spec §26.1),
* deleting is a soft archive by default; permanent deletion is an explicit operation,
* a same-category record with overlapping tags is only marked ``needs_review`` ("possibly
  related"), never declared semantically contradictory (spec §22.11).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import atomic_write_json, read_json
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import (
    LONG_TERM_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    Confidence,
    LongTermRecord,
    LongTermStore,
    MemoryStatus,
    MemoryType,
    SourceKind,
    SourceType,
    TrustLevel,
    utc_now_iso,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.paths import ensure_dir

AuditFn = Callable[..., None]

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _token_set(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class LongTermMemoryStore:
    """File-backed long-term memory with per-record versions."""

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
        self._audit_fn = audit
        self.log = get_logger("long_term_memory")
        self.directory = settings.long_term_dir
        self.path = self.directory / "knowledge.json"

    # ------------------------------------------------------------------ plumbing
    def _audit(self, event_type: str, *, session_id: str = "system", **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id=session_id, **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    def ensure_storage(self) -> None:
        ensure_dir(self.directory, root=self.settings.storage_root, label="long-term directory")
        if not self.path.is_file():
            self._save(LongTermStore())

    def _load(self) -> LongTermStore:
        if not self.path.is_file():
            return LongTermStore()
        try:
            payload = read_json(self.path)
            store = LongTermStore.model_validate(payload)
        except NanoMemoryError:
            raise
        except Exception as exc:
            raise NanoMemoryError(
                code=ErrorCode.CORRUPT_DATA,
                message="Long-term memory file is invalid; refusing to overwrite it.",
                details={"reason": str(exc).splitlines()[0][:200]},
            ) from exc
        supported = SUPPORTED_SCHEMA_VERSIONS["long_term"]
        if store.schema_version > supported:
            raise NanoMemoryError(
                code=ErrorCode.SCHEMA_UNSUPPORTED,
                message="Long-term memory schema is newer than this build supports.",
                details={"found": store.schema_version, "supported": supported},
            )
        return store

    def _save(self, store: LongTermStore) -> None:
        ensure_dir(self.directory, root=self.settings.storage_root, label="long-term directory")
        store.schema_version = LONG_TERM_SCHEMA_VERSION
        store.updated_at = utc_now_iso()
        atomic_write_json(self.path, store.model_dump(mode="json"))

    @property
    def _resource(self) -> str:
        return "long_term"

    def _capacity_used(self, store: LongTermStore) -> int:
        return sum(1 for record in store.records if record.status is not MemoryStatus.ARCHIVED)

    # ------------------------------------------------------------------ create
    def add(
        self,
        payload: dict[str, Any],
        *,
        session_id: str = "system",
        confirm: bool | None = None,
    ) -> LongTermRecord:
        """Add a record after validation, conflict checks and capacity checks.

        ``confirm`` controls the write rule for model-derived memories: without an explicit
        ``True`` they are stored as ``pending_confirmation`` instead of being presented as
        user-stated facts (spec: automatic extraction is not automatic belief).
        """
        with self.locks.acquire([self._resource], operation="long_term_add"):
            store = self._load()
            payload = dict(payload)
            if not payload.get("id") or payload.get("id") == "auto":
                payload["id"] = f"mem-{uuid.uuid4().hex[:16]}"
            payload.setdefault("memory_type", MemoryType.FACT.value)
            outcome = self.validator.validate_long_term_record(payload)
            record: LongTermRecord = outcome.value
            if not record.id or record.id == "auto":
                record.id = f"mem-{uuid.uuid4().hex[:16]}"

            if any(existing.id == record.id for existing in store.records):
                raise NanoMemoryError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="A memory with this id already exists; use update instead.",
                    details={"id": record.id},
                )
            if self._capacity_used(store) >= self.settings.long_term_capacity_limit:
                raise NanoMemoryError(
                    code=ErrorCode.MEMORY_CAPACITY_REACHED,
                    message=(
                        "Long-term memory capacity reached; archive records before adding more."
                    ),
                    details={
                        "capacity_used": self._capacity_used(store),
                        "capacity_limit": self.settings.long_term_capacity_limit,
                    },
                )

            if (
                record.provenance.source_kind is SourceKind.MODEL
                and confirm is not True
                and record.status is MemoryStatus.ACTIVE
            ):
                record = record.model_copy(
                    update={
                        "status": MemoryStatus.PENDING_CONFIRMATION,
                        "review_reason": "model-derived memory requires confirmation",
                    }
                )

            duplicate, related, supersede_ids = self._detect_conflicts(store, record)
            if duplicate:
                record = record.model_copy(
                    update={
                        "status": MemoryStatus.CONFLICT,
                        "related_ids": related,
                        "review_reason": "content duplicate under a different id",
                    }
                )
            elif supersede_ids and record.provenance.source_kind in {
                SourceKind.USER,
                SourceKind.TOOL,
                SourceKind.FILE,
                SourceKind.IMPORT,
            }:
                self._supersede(store, supersede_ids, record.id)
                record = record.model_copy(
                    update={
                        "supersedes": supersede_ids,
                        "related_ids": related,
                        "review_reason": "supersedes older memories with overlapping content",
                    }
                )
            elif related:
                record = record.model_copy(
                    update={
                        "status": MemoryStatus.NEEDS_REVIEW,
                        "related_ids": related,
                        "review_reason": "same category with overlapping tags (possibly related)",
                    }
                )
            store.records.append(record)
            self._save(store)
            self._audit(
                "memory_created",
                session_id=session_id,
                memory_id=record.id,
                status=record.status.value,
                memory_type=record.memory_type.value,
                superseded=supersede_ids,
                secret_hits=outcome.secret_hits,
            )
            return record.model_copy(deep=True)

    def _supersede(self, store: LongTermStore, ids: list[str], new_id: str) -> None:
        """Mark older records superseded by ``new_id``; history is retained."""
        now = utc_now_iso()
        for index, existing in enumerate(store.records):
            if existing.id not in ids:
                continue
            store.records[index] = existing.model_copy(
                update={
                    "status": MemoryStatus.SUPERSEDED,
                    "superseded_by": new_id,
                    "valid_until": now,
                    "updated_at": now,
                    "version": existing.version + 1,
                }
            )

    # ------------------------------------------------------------------ confirmation
    def confirm(self, memory_id: str, *, session_id: str = "system") -> LongTermRecord:
        """Promote a pending memory to active after an explicit confirmation."""
        with self.locks.acquire([self._resource], operation="long_term_confirm"):
            store = self._load()
            index = self._index_of(store, memory_id)
            existing = store.records[index]
            if existing.status is not MemoryStatus.PENDING_CONFIRMATION:
                raise NanoMemoryError(
                    code=ErrorCode.CONFLICTING_STATE
                    if hasattr(ErrorCode, "CONFLICTING_STATE")
                    else ErrorCode.VALIDATION_FAILED,
                    message="Only pending_confirmation memories can be confirmed.",
                    details={"id": memory_id, "status": existing.status.value},
                )
            confirmed = existing.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "review_reason": None,
                    "updated_at": utc_now_iso(),
                    "version": existing.version + 1,
                }
            )
            store.records[index] = confirmed
            self._save(store)
            self._audit(
                "memory_confirmed",
                session_id=session_id,
                memory_id=memory_id,
                version=confirmed.version,
            )
            return confirmed.model_copy(deep=True)

    def reject(
        self, memory_id: str, *, reason: str = "rejected by operator", session_id: str = "system"
    ) -> LongTermRecord:
        """Archive a pending memory instead of deleting it."""
        with self.locks.acquire([self._resource], operation="long_term_reject"):
            store = self._load()
            index = self._index_of(store, memory_id)
            existing = store.records[index]
            rejected = existing.model_copy(
                update={
                    "status": MemoryStatus.ARCHIVED,
                    "review_reason": reason[:500],
                    "updated_at": utc_now_iso(),
                    "version": existing.version + 1,
                }
            )
            store.records[index] = rejected
            self._save(store)
            self._audit(
                "memory_rejected",
                session_id=session_id,
                memory_id=memory_id,
                reason=reason,
            )
            return rejected.model_copy(deep=True)

    def touch_recalled(self, memory_ids: list[str]) -> int:
        """Record that memories were retrieved (utility signal for lifecycle decisions)."""
        if not memory_ids:
            return 0
        with self.locks.acquire([self._resource], operation="long_term_touch"):
            store = self._load()
            now = utc_now_iso()
            touched = 0
            for index, existing in enumerate(store.records):
                if existing.id in memory_ids:
                    store.records[index] = existing.model_copy(
                        update={
                            "recall_count": existing.recall_count + 1,
                            "last_recalled_at": now,
                        }
                    )
                    touched += 1
            if touched:
                self._save(store)
            return touched

    def explain_data(self, memory_id: str) -> dict[str, Any]:
        """Provenance + supersede chain for one memory (the audit trail lives in episodes)."""
        record = self.get(memory_id)
        chain: list[dict[str, Any]] = []
        seen: set[str] = {record.id}
        current = record
        while current.superseded_by and current.superseded_by not in seen:
            seen.add(current.superseded_by)
            try:
                current = self.get(current.superseded_by)
            except NanoMemoryError:
                break
            chain.append(
                {
                    "id": current.id,
                    "status": current.status.value,
                    "version": current.version,
                    "valid_from": current.valid_from,
                    "valid_until": current.valid_until,
                }
            )
        return {
            "record": record.model_dump(mode="json"),
            "provenance": record.provenance.model_dump(mode="json"),
            "supersedes": record.supersedes,
            "superseded_by": record.superseded_by,
            "supersede_chain": chain,
            "related_ids": record.related_ids,
        }

    # ------------------------------------------------------------------ read
    def list_records(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        status: MemoryStatus | None = None,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
        include_archived: bool = False,
        include_superseded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[LongTermRecord]:
        with self.locks.acquire([self._resource], operation="long_term_list"):
            store = self._load()
            records = list(store.records)
        if category:
            records = [record for record in records if record.category == category]
        if tag:
            normalised = tag.strip().lower()
            records = [record for record in records if normalised in record.tags]
        if memory_type is not None:
            records = [record for record in records if record.memory_type is memory_type]
        if user_id is not None:
            records = [record for record in records if record.user_id == user_id]
        if status is not None:
            records = [record for record in records if record.status is status]
        else:
            if not include_archived:
                records = [
                    record for record in records if record.status is not MemoryStatus.ARCHIVED
                ]
            if not include_superseded:
                records = [
                    record for record in records if record.status is not MemoryStatus.SUPERSEDED
                ]
        limit = max(0, min(limit, 500))
        return [record.model_copy(deep=True) for record in records[offset : offset + limit]]

    def get(self, memory_id: str) -> LongTermRecord:
        with self.locks.acquire([self._resource], operation="long_term_get"):
            store = self._load()
            for record in store.records:
                if record.id == memory_id:
                    return record.model_copy(deep=True)
        raise NanoMemoryError(
            code=ErrorCode.MEMORY_NOT_FOUND,
            message="Long-term memory record not found.",
            details={"id": memory_id},
        )

    def search(self, query: str, *, limit: int = 20) -> list[LongTermRecord]:
        """Naive keyword search used by the API; the retriever does scoring."""
        needle = query.strip().lower()
        if not needle:
            return []
        with self.locks.acquire([self._resource], operation="long_term_search"):
            store = self._load()
            matches = [
                record
                for record in store.records
                if record.status
                not in {
                    MemoryStatus.ARCHIVED,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.PENDING_CONFIRMATION,
                }
                and (
                    needle in record.content.lower()
                    or needle in record.category.lower()
                    or any(needle in tag for tag in record.tags)
                )
            ]
        return [record.model_copy(deep=True) for record in matches[: max(0, min(limit, 200))]]

    # ------------------------------------------------------------------ update
    def update(
        self,
        memory_id: str,
        payload: dict[str, Any],
        *,
        if_version: int | None = None,
        session_id: str = "system",
    ) -> LongTermRecord:
        with self.locks.acquire([self._resource], operation="long_term_update"):
            store = self._load()
            index = self._index_of(store, memory_id)
            existing = store.records[index]
            if if_version is not None and existing.version != if_version:
                raise NanoMemoryError(
                    code=ErrorCode.VERSION_CONFLICT,
                    message=f"Long-term memory version conflict for {memory_id}.",
                    details={
                        "id": memory_id,
                        "expected_version": if_version,
                        "current_version": existing.version,
                    },
                )
            merged = existing.model_dump(mode="json")
            for key, value in payload.items():
                if key in {
                    "content",
                    "category",
                    "tags",
                    "source",
                    "source_type",
                    "confidence",
                    "importance",
                    "status",
                }:
                    merged[key] = value
            outcome = self.validator.validate_long_term_record(merged)
            updated: LongTermRecord = outcome.value
            updated.id = existing.id
            updated.created_at = existing.created_at
            updated.updated_at = utc_now_iso()
            updated.version = existing.version + 1
            if (
                updated.status is MemoryStatus.CONFLICT
                and existing.status is not MemoryStatus.CONFLICT
            ):
                updated.status = existing.status
            store.records[index] = updated
            self._save(store)
            self._audit(
                "memory_updated",
                session_id=session_id,
                memory_id=updated.id,
                version=updated.version,
                secret_hits=outcome.secret_hits,
            )
            return updated.model_copy(deep=True)

    # ------------------------------------------------------------------ archive / delete
    def archive(
        self,
        memory_id: str,
        *,
        if_version: int | None = None,
        session_id: str = "system",
    ) -> LongTermRecord:
        with self.locks.acquire([self._resource], operation="long_term_archive"):
            store = self._load()
            index = self._index_of(store, memory_id)
            existing = store.records[index]
            if if_version is not None and existing.version != if_version:
                raise NanoMemoryError(
                    code=ErrorCode.VERSION_CONFLICT,
                    message=f"Long-term memory version conflict for {memory_id}.",
                    details={"id": memory_id, "current_version": existing.version},
                )
            archived = existing.model_copy(
                update={
                    "status": MemoryStatus.ARCHIVED,
                    "updated_at": utc_now_iso(),
                    "version": existing.version + 1,
                }
            )
            store.records[index] = archived
            self._save(store)
            self._audit(
                "memory_updated",
                session_id=session_id,
                memory_id=memory_id,
                status=MemoryStatus.ARCHIVED.value,
                version=archived.version,
                soft_delete=True,
            )
            return archived.model_copy(deep=True)

    def delete(
        self,
        memory_id: str,
        *,
        if_version: int | None = None,
        permanent: bool = False,
        session_id: str = "system",
    ) -> bool:
        """Soft-delete (archive) by default; ``permanent=True`` removes the record."""
        if not permanent:
            self.archive(memory_id, if_version=if_version, session_id=session_id)
            return True
        with self.locks.acquire([self._resource], operation="long_term_delete"):
            store = self._load()
            index = self._index_of(store, memory_id)
            existing = store.records[index]
            if if_version is not None and existing.version != if_version:
                raise NanoMemoryError(
                    code=ErrorCode.VERSION_CONFLICT,
                    message=f"Long-term memory version conflict for {memory_id}.",
                    details={"id": memory_id, "current_version": existing.version},
                )
            del store.records[index]
            self._save(store)
            self._audit(
                "memory_deleted",
                session_id=session_id,
                memory_id=memory_id,
                permanent=True,
            )
            return True

    # ------------------------------------------------------------------ capacity
    def capacity(self) -> dict[str, int]:
        with self.locks.acquire([self._resource], operation="long_term_capacity"):
            store = self._load()
            used = self._capacity_used(store)
        limit = self.settings.long_term_capacity_limit
        return {
            "capacity_used": used,
            "capacity_limit": limit,
            "capacity_percent": int(round(used / limit * 100)) if limit else 0,
        }

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _index_of(store: LongTermStore, memory_id: str) -> int:
        for index, record in enumerate(store.records):
            if record.id == memory_id:
                return index
        raise NanoMemoryError(
            code=ErrorCode.MEMORY_NOT_FOUND,
            message="Long-term memory record not found.",
            details={"id": memory_id},
        )

    def _detect_conflicts(
        self, store: LongTermStore, candidate: LongTermRecord
    ) -> tuple[bool, list[str], list[str]]:
        """Return ``(duplicate, related, supersede_candidates)`` conservatively."""
        normalised = " ".join(candidate.content.lower().split())
        candidate_tokens = _token_set(candidate.content)
        related: list[str] = []
        supersede: list[str] = []
        duplicate = False
        threshold = max(1, self.settings.long_term_tag_overlap_threshold)
        for existing in store.records:
            if existing.status in {MemoryStatus.ARCHIVED, MemoryStatus.SUPERSEDED}:
                continue
            if " ".join(existing.content.lower().split()) == normalised:
                duplicate = True
                related.append(existing.id)
                continue
            similarity = _jaccard(candidate_tokens, _token_set(existing.content))
            if (
                existing.memory_type is candidate.memory_type
                and existing.user_id == candidate.user_id
                and existing.status is MemoryStatus.ACTIVE
                and 0.55 <= similarity < 1.0
            ):
                supersede.append(existing.id)
                related.append(existing.id)
                continue
            if (
                existing.category == candidate.category
                and len(set(existing.tags) & set(candidate.tags)) >= threshold
            ):
                related.append(existing.id)
        return duplicate, related[:20], supersede[:20]

    # ------------------------------------------------------------------ statistics
    def statistics(self) -> dict[str, Any]:
        store = self._load()
        by_status: dict[str, int] = {}
        for record in store.records:
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
        return {
            "records": len(store.records),
            "by_status": by_status,
            "capacity": self.capacity(),
            "schema_version": store.schema_version,
            "updated_at": store.updated_at,
        }

    def export_all(self) -> dict[str, Any]:
        """Full export for backups and data portability (spec §14)."""
        with self.locks.acquire([self._resource], operation="long_term_export"):
            return self._load().model_dump(mode="json")

    def default_record(self, content: str, **overrides: Any) -> LongTermRecord:
        """Test/demo helper building a valid record without persisting it."""
        payload: dict[str, Any] = {
            "id": f"mem-{uuid.uuid4().hex[:16]}",
            "content": content,
            "category": "general",
            "source": "system",
            "source_type": SourceType.SYSTEM.value,
            "confidence": Confidence.MEDIUM.value,
            "importance": 3,
            "tags": [],
            "trust_level": TrustLevel.VERIFIED.value,
        }
        payload.update(overrides)
        return LongTermRecord.model_validate(payload)


__all__ = ["LongTermMemoryStore"]
