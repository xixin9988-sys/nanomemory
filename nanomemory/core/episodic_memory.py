"""Episodic Memory: append-only JSONL with corruption quarantine and corrections.

Design decisions that matter for correctness:

* ``events.jsonl`` is **never rewritten**.  A corrupt line stays where it is, so the audit
  trail remains forensically intact.  The forensic copy is appended to
  ``events.corrupt.jsonl`` (spec §22.4, §24.6).
* Appends are refused while unacknowledged quarantine records exist, unless an admin
  explicitly acknowledges the specific record ids (spec §29.6).  Acknowledgement permits
  appends; it does not declare the damage repaired.
* A ``correction`` event references the original ``event_id`` via ``correction_of``.  The
  default read view hides corrected originals; ``include_corrected=True`` returns the raw
  stream.  A correction whose target is quarantined or missing is *unresolved* and is
  excluded from the "latest facts" view.
* Every append is a single line written under the cross-process episodic lock with
  ``flush`` + ``fsync``.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import (
    EPISODE_SCHEMA_VERSION,
    QUARANTINE_SCHEMA_VERSION,
    Episode,
    EventType,
    QuarantineAck,
    QuarantineRecord,
    TrustLevel,
    utc_now_iso,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.paths import ensure_dir

AuditFn = Callable[..., None]


@dataclass(slots=True)
class EpisodicQuery:
    """Filter for reading episodes (spec §4.3)."""

    session_id: str | None = None
    event_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 200
    include_corrected: bool = False


@dataclass(slots=True)
class EpisodeView:
    """One event plus correction status in the returned view."""

    episode: Episode
    corrected: bool = False
    corrected_by: str | None = None
    target_status: str = "ok"
    correction_status: str | None = None


@dataclass(slots=True)
class EpisodicResult:
    """Episodes, quarantine findings and query diagnostics."""

    events: list[EpisodeView] = field(default_factory=list)
    quarantined: list[QuarantineRecord] = field(default_factory=list)
    corrupt_lines: int = 0
    total_lines: int = 0
    unresolved_corrections: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.corrupt_lines == 0


class EpisodicMemoryStore:
    """Append-only episodic events with quarantine and correction semantics."""

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
        self.log = get_logger("episodic_memory")
        self.directory = settings.episodic_dir
        self.events_path = self.directory / "events.jsonl"
        self.corrupt_path = self.directory / "events.corrupt.jsonl"
        self.acks_path = self.directory / "quarantine_acks.json"

    # ------------------------------------------------------------------ plumbing
    def ensure_storage(self) -> None:
        ensure_dir(self.directory, root=self.settings.storage_root, label="episodic directory")
        if not self.events_path.exists():
            self.events_path.touch()

    def _audit(self, event_type: str, *, session_id: str = "system", **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id=session_id, **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    @property
    def _resource(self) -> str:
        return "episodic"

    # ------------------------------------------------------------------ append
    def append(
        self,
        *,
        session_id: str,
        event_type: str,
        summary: str = "",
        related_memory_ids: list[str] | None = None,
        source: str = "system",
        outcome: str = "recorded",
        payload: dict[str, Any] | None = None,
        correction_of: str | None = None,
        trust_level: TrustLevel = TrustLevel.UNKNOWN,
        allow_quarantined: bool = False,
    ) -> Episode:
        """Append one event, refusing while unacknowledged quarantine exists."""
        episode = Episode(
            event_id=f"evt-{uuid.uuid4().hex[:20]}",
            session_id=session_id,
            event_type=event_type,
            summary=summary,
            related_memory_ids=related_memory_ids or [],
            source=source,
            outcome=outcome,
            correction_of=correction_of,
            trust_level=trust_level,
            payload=payload,
        )
        validated = self.validator.validate_episode(episode).value
        with self.locks.acquire([self._resource], operation="episode_append"):
            self.ensure_storage()
            pending = self._pending_quarantine_locked()
            if pending and not allow_quarantined:
                raise NanoMemoryError(
                    code=ErrorCode.QUARANTINE_UNACKNOWLEDGED,
                    message=(
                        "Episodic memory has unrepaired quarantined records; acknowledge them "
                        "before appending new events."
                    ),
                    details={"pending_records": len(pending)},
                )
            line = json.dumps(validated.model_dump(mode="json"), ensure_ascii=False)
            if "\n" in line:  # pragma: no cover - json.dumps never emits raw newlines
                raise NanoMemoryError(
                    code=ErrorCode.VALIDATION_FAILED, message="event cannot be serialised"
                )
            try:
                with open(self.events_path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except PermissionError as exc:
                raise NanoMemoryError(
                    code=ErrorCode.PERMISSION_DENIED,
                    message="Cannot append to the episodic log: permission denied.",
                ) from exc
            except OSError as exc:
                raise NanoMemoryError(
                    code=ErrorCode.IO_ERROR,
                    message="Cannot append to the episodic log.",
                    details={"errno": exc.errno},
                ) from exc
        return validated

    # ------------------------------------------------------------------ read
    def read(self, query: EpisodicQuery) -> EpisodicResult:
        with self.locks.acquire([self._resource], operation="episode_read"):
            self.ensure_storage()
            episodes, quarantined, total, corrupt = self._parse_locked()
            pending = self._pending_quarantine_locked()
        result = EpisodicResult(
            quarantined=pending,
            corrupt_lines=corrupt,
            total_lines=total,
        )
        selected = [ep for ep in episodes if self._matches(ep, query)]
        by_id = {ep.event_id: ep for ep in episodes}
        all_quarantine = self.quarantine_records()
        quarantined_ids = {record.event_id for record in all_quarantine if record.event_id}

        corrections: dict[str, Episode] = {}
        for episode in episodes:
            if not episode.correction_of:
                continue
            if episode.correction_of in quarantined_ids:
                result.unresolved_corrections.append(episode.event_id)
                continue
            target = by_id.get(episode.correction_of)
            if target is None:
                result.unresolved_corrections.append(episode.event_id)
                continue
            corrections[episode.correction_of] = episode

        for episode in selected:
            view = EpisodeView(episode=episode)
            if episode.event_id in corrections:
                view.corrected = True
                view.corrected_by = corrections[episode.event_id].event_id
                if not query.include_corrected:
                    continue
            if episode.correction_of:
                if episode.correction_of in quarantined_ids:
                    view.target_status = "quarantined"
                    view.correction_status = "unresolved"
                    if not query.include_corrected:
                        continue
                    result.events.append(view)
                    continue
                if episode.correction_of not in by_id:
                    view.target_status = "missing"
                    view.correction_status = "unresolved"
                    if not query.include_corrected:
                        continue
                    result.events.append(view)
                    continue
                view.target_status = "ok"
                view.correction_status = "applied"
            result.events.append(view)

        result.events.sort(key=lambda view: view.episode.timestamp, reverse=True)
        limit = max(1, min(query.limit, 1000))
        result.events = result.events[:limit]
        return result

    @staticmethod
    def _matches(episode: Episode, query: EpisodicQuery) -> bool:
        if query.session_id and episode.session_id != query.session_id:
            return False
        if query.event_type and episode.event_type != query.event_type:
            return False
        stamp = _parse_timestamp(episode.timestamp)
        if query.since and stamp and stamp < query.since:
            return False
        if query.until and stamp and stamp > query.until:
            return False
        return True

    # ------------------------------------------------------------------ corruption
    def scan(self) -> EpisodicResult:
        """Parse the JSONL, quarantining corrupt lines without touching the original."""
        with self.locks.acquire([self._resource], operation="episode_scan"):
            self.ensure_storage()
            episodes, quarantined, total, corrupt = self._parse_locked()
            pending = self._pending_quarantine_locked()
        return EpisodicResult(
            events=[EpisodeView(episode=episode) for episode in episodes],
            quarantined=pending,
            corrupt_lines=corrupt,
            total_lines=total,
        )

    def _parse_locked(self) -> tuple[list[Episode], list[QuarantineRecord], int, int]:
        episodes: list[Episode] = []
        total = 0
        corrupt = 0
        new_records: list[QuarantineRecord] = []
        if not self.events_path.is_file():
            return episodes, [], 0, 0
        known_quarantine = self.quarantine_records()
        with open(self.events_path, "rb") as handle:
            for line_no, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                total += 1
                try:
                    text = raw.decode("utf-8")
                    payload = json.loads(text)
                    episode = Episode.model_validate(payload)
                    if episode.schema_version > EPISODE_SCHEMA_VERSION:
                        raise ValueError("episode schema_version is newer than supported")
                    episodes.append(episode)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                    corrupt += 1
                    record = self._quarantine_line(line_no, raw, known_quarantine)
                    if record is not None:
                        new_records.append(record)
                        known_quarantine.append(record)
        if new_records:
            self._append_corrupt_records(new_records)
        return episodes, new_records, total, corrupt

    def _quarantine_line(
        self,
        line_no: int,
        raw: bytes,
        existing: list[QuarantineRecord],
    ) -> QuarantineRecord | None:
        content_hash = _sha(raw)
        if any(
            record.line_no == line_no and record.content_hash == content_hash for record in existing
        ):
            return None
        try:
            text = raw.decode("utf-8")
            encoded, encoding = text, "utf-8"
        except UnicodeDecodeError:
            text = None
            encoded, encoding = base64.b64encode(raw).decode("ascii"), "base64"
        event_id: str | None = None
        if text is not None:
            try:
                candidate = json.loads(text)
                if isinstance(candidate, dict) and isinstance(candidate.get("event_id"), str):
                    event_id = candidate["event_id"][:80]
            except json.JSONDecodeError:
                event_id = None
        record = QuarantineRecord(
            schema_version=QUARANTINE_SCHEMA_VERSION,
            quarantine_id=f"qr-{uuid.uuid4().hex[:20]}",
            source_file="events.jsonl",
            line_no=line_no,
            raw_line=encoded,
            raw_encoding=encoding,  # type: ignore[arg-type]
            reason="line is not a valid episode record",
            content_hash=content_hash,
            event_id=event_id,
        )
        self._audit(
            "quarantine_detected",
            session_id="system",
            quarantine_id=record.quarantine_id,
            line_no=line_no,
            reason=record.reason,
        )
        return record

    def _append_corrupt_records(self, records: list[QuarantineRecord]) -> None:
        try:
            with open(self.corrupt_path, "a", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(
                        json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:  # pragma: no cover - quarantine write failure is serious
            raise NanoMemoryError(
                code=ErrorCode.IO_ERROR,
                message="Cannot write the quarantine file.",
                details={"errno": exc.errno},
            ) from exc

    def quarantine_records(self) -> list[QuarantineRecord]:
        """All quarantine records read from the sidecar (no lock; caller holds or accepts)."""
        if not self.corrupt_path.is_file():
            return []
        records: list[QuarantineRecord] = []
        with open(self.corrupt_path, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    records.append(QuarantineRecord.model_validate(json.loads(raw.decode("utf-8"))))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    continue
        return records

    # ------------------------------------------------------------------ acknowledgement
    def _acks(self) -> list[QuarantineAck]:
        if not self.acks_path.is_file():
            return []
        try:
            payload = json.loads(self.acks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        acks: list[QuarantineAck] = []
        for item in payload.get("acks", []):
            try:
                acks.append(QuarantineAck.model_validate(item))
            except ValueError:
                continue
        return acks

    def _pending_quarantine_locked(self) -> list[QuarantineRecord]:
        acknowledged = {ack.quarantine_id for ack in self._acks()}
        return [
            record
            for record in self.quarantine_records()
            if record.quarantine_id not in acknowledged
        ]

    def pending_quarantine(self) -> list[QuarantineRecord]:
        with self.locks.acquire([self._resource], operation="episode_quarantine"):
            self.ensure_storage()
            return self._pending_quarantine_locked()

    def acknowledge(
        self,
        quarantine_ids: list[str],
        *,
        reason: str,
        operator: str = "local-admin",
        request_id: str | None = None,
    ) -> list[QuarantineAck]:
        """Authorise further appends for the named records (never a repair)."""
        if not quarantine_ids:
            raise NanoMemoryError(
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="At least one quarantine record id is required.",
            )
        if len(reason.strip()) < 3:
            raise NanoMemoryError(
                code=ErrorCode.CONFIRMATION_REQUIRED,
                message="An explicit reason is required for acknowledgement.",
            )
        with self.locks.acquire([self._resource], operation="quarantine_acknowledge"):
            self.ensure_storage()
            known = {record.quarantine_id for record in self.quarantine_records()}
            unknown = [item for item in quarantine_ids if item not in known]
            if unknown:
                raise NanoMemoryError(
                    code=ErrorCode.NOT_FOUND,
                    message="Unknown quarantine record id.",
                    details={"unknown_ids": unknown[:10]},
                )
            existing = self._acks()
            existing_ids = {ack.quarantine_id for ack in existing}
            created: list[QuarantineAck] = []
            for quarantine_id in quarantine_ids:
                if quarantine_id in existing_ids:
                    continue
                ack = QuarantineAck(
                    quarantine_id=quarantine_id,
                    reason=reason.strip(),
                    operator=operator,
                    request_id=request_id,
                )
                existing.append(ack)
                created.append(ack)
            payload = {
                "schema_version": 1,
                "updated_at": utc_now_iso(),
                "acks": [ack.model_dump(mode="json") for ack in existing],
            }
            tmp = self.acks_path.with_name(
                f".{self.acks_path.name}.nanomemory-tmp.{os.getpid()}.{uuid.uuid4().hex}"
            )
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.acks_path)
            for ack in created:
                self._audit(
                    "quarantine_acknowledged",
                    session_id="system",
                    quarantine_id=ack.quarantine_id,
                    reason=ack.reason,
                    operator=ack.operator,
                    request_id=ack.request_id,
                )
            return created

    # ------------------------------------------------------------------ statistics
    def statistics(self) -> dict[str, Any]:
        with self.locks.acquire([self._resource], operation="episode_stats"):
            self.ensure_storage()
            episodes, quarantined, total, corrupt = self._parse_locked()
            pending = self._pending_quarantine_locked()
        by_type: dict[str, int] = {}
        for episode in episodes:
            by_type[episode.event_type] = by_type.get(episode.event_type, 0) + 1
        return {
            "events": len(episodes),
            "total_lines": total,
            "corrupt_lines": corrupt,
            "quarantined": len(quarantined),
            "pending_quarantine": len(pending),
            "by_event_type": by_type,
        }

    # ------------------------------------------------------------------ audit helper
    def record_audit(
        self,
        *,
        event_type: str,
        session_id: str,
        summary: str = "",
        related_memory_ids: list[str] | None = None,
        outcome: str = "ok",
        payload: dict[str, Any] | None = None,
        allow_quarantined: bool = True,
    ) -> Episode:
        """Append an audit event; never raises on quarantine state (it *is* the audit)."""
        return self.append(
            session_id=session_id,
            event_type=event_type,
            summary=summary or event_type,
            related_memory_ids=related_memory_ids,
            source="nanomemory",
            outcome=outcome,
            payload=payload,
            trust_level=TrustLevel.VERIFIED,
            allow_quarantined=allow_quarantined,
        )


def _sha(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def _line_hash(episode: Episode) -> str:
    """Hash of the canonical JSON form, used to detect "this event is quarantined"."""
    import hashlib

    canonical = json.dumps(episode.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


__all__ = [
    "EpisodeView",
    "EpisodicMemoryStore",
    "EpisodicQuery",
    "EpisodicResult",
    "EventType",
]
