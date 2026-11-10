"""Pydantic models for every persisted format.

The models are the single source of truth for field names, types, ranges and
``schema_version`` values.  Storage modules serialise through these models so an invalid
record can never reach disk (spec §22.11, §25.5, §26.2).

Working Memory markdown is a *human readable* projection; its metadata lives in the sidecar
``memory.meta.json`` and is never parsed out of the markdown body (spec §25.1).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- versions
WORKING_SCHEMA_VERSION = 1
WORKING_META_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
LONG_TERM_SCHEMA_VERSION = 1
EPISODE_SCHEMA_VERSION = 1
QUARANTINE_SCHEMA_VERSION = 1
BACKUP_SCHEMA_VERSION = 1
USAGE_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 1

#: Highest schema version this build understands, per format.
SUPPORTED_SCHEMA_VERSIONS: dict[str, int] = {
    "working": WORKING_SCHEMA_VERSION,
    "working_meta": WORKING_META_SCHEMA_VERSION,
    "journal": JOURNAL_SCHEMA_VERSION,
    "long_term": LONG_TERM_SCHEMA_VERSION,
    "episode": EPISODE_SCHEMA_VERSION,
    "quarantine": QUARANTINE_SCHEMA_VERSION,
    "backup": BACKUP_SCHEMA_VERSION,
    "usage": USAGE_SCHEMA_VERSION,
    "session": SESSION_SCHEMA_VERSION,
}


def utc_now() -> datetime:
    """Timezone-aware UTC now (all persisted timestamps use this)."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp string used in persisted records."""
    return utc_now().isoformat()


class GoalStatus(StrEnum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    CONFLICT = "conflict"
    NEEDS_REVIEW = "needs_review"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    USER_PROVIDED = "user_provided"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    USER = "user"
    MODEL = "model"
    TOOL = "tool"
    SYSTEM = "system"
    IMPORT = "import"
    UNKNOWN = "unknown"


class JournalStatus(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class SessionState(StrEnum):
    ACTIVE = "active"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_PENDING = "recovery_pending"
    MAINTENANCE = "maintenance"


class EventType(StrEnum):
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    DECISION_MADE = "decision_made"
    MEMORY_CREATED = "memory_created"
    MEMORY_UPDATED = "memory_updated"
    MEMORY_CONFLICT = "memory_conflict"
    MEMORY_DELETED = "memory_deleted"
    TOOL_EXECUTED = "tool_executed"
    ERROR_OCCURRED = "error_occurred"
    CORRECTION = "correction"
    AGENT_RUN = "agent_run"
    BACKUP_CREATED = "backup_created"
    RESTORE_STARTED = "restore_started"
    RESTORE_COMPLETED = "restore_completed"
    RESTORE_FAILED = "restore_failed"
    MIGRATION_STARTED = "migration_started"
    MIGRATION_COMPLETED = "migration_completed"
    MIGRATION_FAILED = "migration_failed"
    MIGRATION_ROLLBACK_COMPLETED = "migration_rollback_completed"
    MIGRATION_RECOVERY_REQUIRED = "migration_recovery_required"
    TRANSACTION_PREPARE_FAILED = "transaction_prepare_failed"
    TRANSACTION_ROLLBACK_COMPLETED = "transaction_rollback_completed"
    RECOVERY_REQUIRED_ENTERED = "recovery_required_entered"
    RECOVERY_COMPLETED = "recovery_completed"
    RECOVERY_EXTERNAL_MODIFICATION_DETECTED = "recovery_external_modification_detected"
    ROLLBACK_COMPLETED = "rollback_completed"
    ROLLED_BACK_CLEANUP_COMPLETED = "rolled_back_cleanup_completed"
    ROLLED_BACK_STATE_INCONSISTENT = "rolled_back_state_inconsistent"
    ORPHAN_TXN_CLEANED = "orphan_txn_cleaned"
    ORPHAN_TXN_PARTIAL_COMMIT_DETECTED = "orphan_txn_with_partial_commit_detected"
    ORPHAN_TEMP_CLEANED = "orphan_temp_cleaned"
    JOURNAL_TEMP_CLEANED = "journal_temp_cleaned"
    QUARANTINE_DETECTED = "quarantine_detected"
    QUARANTINE_ACKNOWLEDGED = "quarantine_acknowledged"
    SESSION_CREATED = "session_created"
    SESSION_RECOVERY_PENDING = "session_recovery_pending"
    AUDIT = "audit"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- working
class WorkingMemoryDocument(BaseModel):
    """Human readable Working Memory content (persisted as ``memory.md``)."""

    model_config = ConfigDict(extra="forbid")

    current_goal: str = Field(default="尚未設定", max_length=2000)
    status: GoalStatus = GoalStatus.IDLE
    relevant_context: list[str] = Field(default_factory=list, max_length=50)
    completed: list[str] = Field(default_factory=list, max_length=100)
    in_progress: list[str] = Field(default_factory=list, max_length=100)
    blockers: list[str] = Field(default_factory=list, max_length=50)
    next_action: str = Field(default="等待使用者輸入", max_length=2000)

    @field_validator("relevant_context", "completed", "in_progress", "blockers")
    @classmethod
    def _item_limits(cls, value: list[str]) -> list[str]:
        if any(len(item) > 1000 for item in value):
            raise ValueError("each list item must be at most 1000 characters")
        return value

    @model_validator(mode="after")
    def _consistent_status(self) -> WorkingMemoryDocument:
        if self.status is GoalStatus.BLOCKED and not self.blockers:
            # Allowed, but make the intent explicit rather than silently inconsistent.
            self.blockers = ["未提供阻礙原因（狀態為 blocked）"]
        return self


SECTION_TITLES: dict[str, str] = {
    "current_goal": "Current Goal",
    "status": "Status",
    "relevant_context": "Relevant Context",
    "completed": "Completed",
    "in_progress": "In Progress",
    "blockers": "Blockers",
    "next_action": "Next Action",
}

_LIST_FIELDS = ("relevant_context", "completed", "in_progress", "blockers")
_EMPTY_MARKER = "尚無"


def render_working_markdown(document: WorkingMemoryDocument) -> str:
    """Render the document to the canonical markdown body."""
    lines: list[str] = ["# Working Memory", ""]
    for field, title in SECTION_TITLES.items():
        lines.append(f"## {title}")
        lines.append("")
        value = getattr(document, field)
        if isinstance(value, list):
            items = value or [_EMPTY_MARKER]
            lines.extend(f"- {item}" for item in items)
        else:
            rendered = value.value if isinstance(value, StrEnum) else str(value)
            lines.append(rendered)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


def parse_working_markdown(text: str) -> WorkingMemoryDocument:
    """Parse the canonical markdown body back into a document.

    Raises :class:`ValueError` when required sections are missing or unparseable; callers
    map that to the ``corrupt_data`` error code and never silently substitute defaults.
    """
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise ValueError("working memory markdown has no '## ' sections")
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group("title").strip().lower()] = text[match.end() : end].strip()

    values: dict[str, Any] = {}
    for field, title in SECTION_TITLES.items():
        raw = sections.get(title.lower())
        if raw is None:
            raise ValueError(f"working memory markdown is missing the '{title}' section")
        if field in _LIST_FIELDS:
            items = [line[2:].strip() for line in raw.splitlines() if line.strip().startswith("- ")]
            items = [item for item in items if item != _EMPTY_MARKER]
            values[field] = items
        else:
            values[field] = raw.splitlines()[0].strip() if raw else ""
    return WorkingMemoryDocument(**values)


class WorkingMemoryMeta(BaseModel):
    """Sidecar metadata: the only authoritative source of version and content hash."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = WORKING_META_SCHEMA_VERSION
    memory_version: int = Field(ge=0)
    session_id: str
    updated_at: str = Field(default_factory=utc_now_iso)
    content_sha256: str = Field(min_length=64, max_length=64)
    status: SessionState = SessionState.ACTIVE


class WorkingMemorySnapshot(BaseModel):
    """Read model returned to callers (document + metadata + raw markdown)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    document: WorkingMemoryDocument
    meta: WorkingMemoryMeta
    markdown: str


class MemoryUpdateProposal(BaseModel):
    """Structured proposal produced by the agent or submitted through the API."""

    model_config = ConfigDict(extra="forbid")

    current_goal: str | None = Field(default=None, max_length=2000)
    status: GoalStatus | None = None
    relevant_context: list[str] | None = None
    completed: list[str] | None = None
    in_progress: list[str] | None = None
    blockers: list[str] | None = None
    next_action: str | None = Field(default=None, max_length=2000)
    allow_task_removal: bool = False
    """Explicit opt-in required to drop an unfinished ``in_progress`` item (§4.1)."""

    def apply_to(self, document: WorkingMemoryDocument) -> WorkingMemoryDocument:
        """Merge the proposal onto ``document`` without mutating the input."""
        update = {
            field: getattr(self, field)
            for field in SECTION_TITLES
            if getattr(self, field) is not None
        }
        return document.model_copy(update=update)


# --------------------------------------------------------------------------- journal
class TxnJournal(BaseModel):
    """Working Memory transaction journal (spec §28.1, §36.1).

    Paths in ``new_md_tmp`` / ``new_meta_tmp`` / ``old_*_snapshot`` are relative names inside
    ``.txn/<txn_id>/`` so the journal never leaks absolute paths.
    """

    model_config = ConfigDict(extra="forbid")

    journal_schema_version: int = JOURNAL_SCHEMA_VERSION
    txn_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_id: str
    old_exists: bool
    old_md_sha256: str | None = Field(default=None, max_length=64)
    old_meta_sha256: str | None = Field(default=None, max_length=64)
    new_md_sha256: str = Field(min_length=64, max_length=64)
    new_meta_sha256: str = Field(min_length=64, max_length=64)
    new_md_tmp: str = "new_memory.md"
    new_meta_tmp: str = "new_memory.meta.json"
    old_md_snapshot: str = "old_memory.md"
    old_meta_snapshot: str = "old_memory.meta.json"
    status: JournalStatus = JournalStatus.PREPARED
    started_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None

    @model_validator(mode="after")
    def _old_hashes_consistent(self) -> TxnJournal:
        if self.old_exists and not (self.old_md_sha256 and self.old_meta_sha256):
            raise ValueError("old_exists=true requires both old hashes")
        if not self.old_exists and (self.old_md_sha256 or self.old_meta_sha256):
            raise ValueError("old_exists=false requires null old hashes")
        return self


# --------------------------------------------------------------------------- long term
class LongTermRecord(BaseModel):
    """One long-term memory record with per-record version and provenance."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=8, max_length=80)
    content: str = Field(min_length=1, max_length=20_000)
    category: str = Field(min_length=1, max_length=80)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    source: str = Field(min_length=1, max_length=500)
    source_type: SourceType = SourceType.UNKNOWN
    confidence: Confidence = Confidence.MEDIUM
    importance: int = Field(default=3, ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=30)
    status: MemoryStatus = MemoryStatus.ACTIVE
    trust_level: TrustLevel = TrustLevel.UNKNOWN
    version: int = Field(default=1, ge=1)
    schema_version: int = LONG_TERM_SCHEMA_VERSION
    related_ids: list[str] = Field(default_factory=list, max_length=50)
    review_reason: str | None = Field(default=None, max_length=500)

    @field_validator("tags")
    @classmethod
    def _tag_shape(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            normalised = tag.strip().lower()
            if not normalised:
                continue
            if len(normalised) > 40:
                raise ValueError("tags must be at most 40 characters")
            if normalised not in cleaned:
                cleaned.append(normalised)
        return cleaned


class LongTermStore(BaseModel):
    """Root document of ``knowledge.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = LONG_TERM_SCHEMA_VERSION
    updated_at: str = Field(default_factory=utc_now_iso)
    records: list[LongTermRecord] = Field(default_factory=list)


# --------------------------------------------------------------------------- episodic
class Episode(BaseModel):
    """One line of ``events.jsonl`` (append-only)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=80)
    timestamp: str = Field(default_factory=utc_now_iso)
    session_id: str
    event_type: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=4000)
    related_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    source: str = Field(default="system", max_length=200)
    outcome: str = Field(default="recorded", max_length=100)
    schema_version: int = EPISODE_SCHEMA_VERSION
    correction_of: str | None = Field(default=None, max_length=80)
    trust_level: TrustLevel = TrustLevel.UNKNOWN
    payload: dict[str, Any] | None = None

    @field_validator("event_type")
    @classmethod
    def _known_event(cls, value: str) -> str:
        # Unknown event types are accepted (forward compatible) but must look like a slug.
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", value):
            raise ValueError("event_type must be a lowercase slug")
        return value


class QuarantineRecord(BaseModel):
    """A quarantined JSONL line; the original file is never modified (spec §24.6)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = QUARANTINE_SCHEMA_VERSION
    quarantine_id: str = Field(min_length=8, max_length=80)
    source_file: str
    line_no: int = Field(ge=1)
    raw_line: str
    raw_encoding: Literal["utf-8", "base64"] = "utf-8"
    reason: str = Field(max_length=300)
    detected_at: str = Field(default_factory=utc_now_iso)
    content_hash: str = Field(min_length=64, max_length=64)
    session_id: str | None = None
    event_id: str | None = Field(default=None, max_length=80)
    """Best-effort event id of the quarantined line (used for correction resolution)."""


class QuarantineAck(BaseModel):
    """Acknowledgement that allows appends despite unrepaired quarantine records."""

    model_config = ConfigDict(extra="forbid")

    quarantine_id: str
    acknowledged_at: str = Field(default_factory=utc_now_iso)
    reason: str = Field(min_length=3, max_length=500)
    operator: str = Field(default="local-admin", max_length=200)
    request_id: str | None = None


# --------------------------------------------------------------------------- backup
class BackupFileEntry(BaseModel):
    """One file inside a backup with its integrity hash."""

    model_config = ConfigDict(extra="forbid")

    rel_path: str
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    memory_version: int | None = None
    schema_version: int | None = None


class BackupManifest(BaseModel):
    """``manifest.json`` for every backup (spec §22.5)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = BACKUP_SCHEMA_VERSION
    backup_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    created_at: str = Field(default_factory=utc_now_iso)
    label: str = Field(default="manual", max_length=120)
    product_version: str = Field(default="0.1.0", max_length=40)
    schema_versions: dict[str, int] = Field(default_factory=dict)
    files: list[BackupFileEntry] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    restore_txn_id: str | None = None


# --------------------------------------------------------------------------- metrics
class UsageRecord(BaseModel):
    """One metrics line; ``usage_source`` distinguishes exact usage from estimates."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = USAGE_SCHEMA_VERSION
    request_id: str
    timestamp: str = Field(default_factory=utc_now_iso)
    provider: str
    model: str
    usage_source: Literal["exact", "estimated", "unknown", "mock"]
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, max_length=8)
    session_id: str | None = None
    kind: str = Field(default="agent", max_length=40)
    benchmark: str | None = Field(default=None, max_length=80)
    estimator: str | None = Field(default=None, max_length=80)


# --------------------------------------------------------------------------- sessions
class SessionMeta(BaseModel):
    """Session registry entry stored in ``<session>/.session.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SESSION_SCHEMA_VERSION
    session_id: str
    created_at: str = Field(default_factory=utc_now_iso)
    status: SessionState = SessionState.ACTIVE
    last_seen_at: str | None = None


class SessionStateRecord(BaseModel):
    """Durable per-session health state (``.state.json``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SESSION_SCHEMA_VERSION
    session_id: str
    state: SessionState = SessionState.ACTIVE
    reason: str | None = Field(default=None, max_length=500)
    txn_id: str | None = Field(default=None, max_length=64)
    updated_at: str = Field(default_factory=utc_now_iso)


# --------------------------------------------------------------------------- maintenance
class MaintenanceMarker(BaseModel):
    """Durable maintenance flag (spec §28.2, §34.4).  Existence means ``maintenance_mode``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    migration_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["active", "rolling_back"] = "active"
    started_at: str = Field(default_factory=utc_now_iso)
    reason: str = Field(default="schema_migration", max_length=120)
    pid: int = 0
    from_versions: dict[str, int] = Field(default_factory=dict)


class MigrationJournal(BaseModel):
    """One migration attempt; enables startup recovery and audit (spec §26.2)."""

    model_config = ConfigDict(extra="forbid")

    journal_schema_version: int = 1
    migration_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    format_name: str
    from_version: int
    to_version: int
    source_rel_path: str
    backup_rel_path: str | None = None
    source_hash: str | None = Field(default=None, max_length=64)
    target_hash: str | None = Field(default=None, max_length=64)
    status: Literal["started", "completed", "failed", "rolled_back"] = "started"
    started_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None


# --------------------------------------------------------------------------- agent
class ToolCallRequest(BaseModel):
    """Tool call requested by the model (validated before execution)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Structured tool outcome returned to the model and recorded in episodes."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    data: Any = None
    error_code: str | None = None
    message: str | None = None
    duration_ms: float | None = None


class AgentMemoryUpdate(BaseModel):
    """``memory_update`` block of the structured agent response."""

    model_config = ConfigDict(extra="forbid")

    current_goal: str | None = Field(default=None, max_length=2000)
    status: GoalStatus | None = None
    completed: list[str] | None = None
    in_progress: list[str] | None = None
    blockers: list[str] | None = None
    next_action: str | None = Field(default=None, max_length=2000)
    relevant_context: list[str] | None = None
    source: str = Field(default="agent", max_length=200)
    notes: str | None = Field(default=None, max_length=1000)


class AgentResponse(BaseModel):
    """Structured model output (spec §8)."""

    model_config = ConfigDict(extra="forbid")

    response: str = Field(min_length=1, max_length=20_000)
    task_status: TaskStatus = TaskStatus.IN_PROGRESS
    next_action: str | None = Field(default=None, max_length=2000)
    tool_calls: list[ToolCallRequest] = Field(default_factory=list, max_length=10)
    memory_update: AgentMemoryUpdate | None = None


class RetrievedMemory(BaseModel):
    """A single memory item selected by the retriever with its justification."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["working", "long_term", "episodic"]
    id: str
    content: str
    score: float = 0.0
    reason: str = ""
    source: str = "unknown"
    trust_level: TrustLevel = TrustLevel.UNKNOWN
    tokens: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """Retriever output (spec §6)."""

    model_config = ConfigDict(extra="forbid")

    current_task: str
    session_id: str
    selected: list[RetrievedMemory] = Field(default_factory=list)
    omitted: list[dict[str, Any]] = Field(default_factory=list)
    estimated_tokens: int = 0
    token_budget: int = 0
    retrieval_reason: str = ""
    truncated_for_budget: bool = False


__all__ = [
    "BACKUP_SCHEMA_VERSION",
    "EPISODE_SCHEMA_VERSION",
    "JOURNAL_SCHEMA_VERSION",
    "LONG_TERM_SCHEMA_VERSION",
    "QUARANTINE_SCHEMA_VERSION",
    "SESSION_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "USAGE_SCHEMA_VERSION",
    "WORKING_META_SCHEMA_VERSION",
    "WORKING_SCHEMA_VERSION",
    "AgentMemoryUpdate",
    "AgentResponse",
    "BackupFileEntry",
    "BackupManifest",
    "Confidence",
    "Episode",
    "EventType",
    "GoalStatus",
    "JournalStatus",
    "LongTermRecord",
    "LongTermStore",
    "MaintenanceMarker",
    "MemoryStatus",
    "MemoryUpdateProposal",
    "MigrationJournal",
    "QuarantineAck",
    "QuarantineRecord",
    "RetrievalResult",
    "RetrievedMemory",
    "SECTION_TITLES",
    "SessionMeta",
    "SessionState",
    "SessionStateRecord",
    "SourceType",
    "TaskStatus",
    "ToolCallRequest",
    "ToolResult",
    "TrustLevel",
    "TxnJournal",
    "UsageRecord",
    "WorkingMemoryDocument",
    "WorkingMemoryMeta",
    "WorkingMemorySnapshot",
    "parse_working_markdown",
    "render_working_markdown",
    "utc_now",
    "utc_now_iso",
]
