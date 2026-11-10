"""Memory Controller: the single entry point for every memory operation (spec §5).

Responsibilities:

* own the lock manager, stores, validator, providers, agent loop, metrics and reports,
* expose the documented controller operations (``initialize_storage``,
  ``read_working_memory``, ``propose_memory_update``, ``validate_memory_update``,
  ``apply_memory_update``, ``add_long_term_memory``, ``retrieve_long_term_memory``,
  ``append_episode``, ``create_backup``, ``restore_backup``, ``get_memory_statistics``),
* enforce maintenance mode and session rules at one choke point,
* keep auditing non-recursive: the episodic store is the audit sink, never audited itself.

The API, dashboard and benchmarks talk to this object (the dashboard only through HTTP) so
there is exactly one implementation of validation, locking and version checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nanomemory.core.agent_loop import AgentLoop, AgentRunResult, CancellationToken
from nanomemory.core.backup import BackupService, RestoreResult
from nanomemory.core.config import (
    DEFAULT_SESSION_ID,
    Settings,
    get_settings,
)
from nanomemory.core.context_builder import ContextBuilder
from nanomemory.core.episodic_memory import EpisodicMemoryStore, EpisodicQuery, EpisodicResult
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.health import HealthReport, HealthService, health_payload
from nanomemory.core.lifecycle import run_lifecycle
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import configure_logging, get_logger
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_compressor import CompressionResult, MemoryCompressor
from nanomemory.core.memory_formation import MemoryFormation
from nanomemory.core.memory_models import (
    Episode,
    LongTermRecord,
    MemoryType,
    QuarantineAck,
    QuarantineRecord,
    RetrievalResult,
    SessionMeta,
    SourceKind,
    UsageRecord,
    WorkingMemorySnapshot,
)
from nanomemory.core.memory_retriever import MemoryRetriever
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.metrics import MetricsStore, MetricsSummary
from nanomemory.core.migrations import MigrationManager, MigrationReport
from nanomemory.core.paths import ensure_dir
from nanomemory.core.recovery import RecoveryEngine, StartupReport
from nanomemory.core.sessions import SessionRegistry
from nanomemory.core.token_counter import TokenCounter
from nanomemory.core.tools import ToolContext, ToolRegistry, build_default_registry
from nanomemory.core.working_memory import UpdateResult, WorkingMemoryStore
from nanomemory.providers.base import BaseProvider
from nanomemory.providers.mock_provider import MockProvider


@dataclass(slots=True)
class StorageReport:
    """Result of :meth:`MemoryManager.initialize_storage`."""

    storage_root: str
    default_session_id: str
    created: list[str] = field(default_factory=list)
    schema_versions: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_root": self.storage_root,
            "default_session_id": self.default_session_id,
            "created": self.created,
            "schema_versions": self.schema_versions,
        }


class MemoryManager:
    """Wires stores, locks, providers and the agent loop together."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: BaseProvider | None = None,
        configure_logs: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        if configure_logs:
            configure_logging(self.settings.log_level, self.settings.log_format)
        self.log = get_logger("manager")

        self.locks = LockManager(
            self.settings.locks_dir,
            storage_root=self.settings.storage_root,
            default_timeout=self.settings.lock_timeout_seconds,
        )
        self.validator = MemoryValidator(self.settings)
        self.registry = SessionRegistry(self.settings)

        # Episodic memory is the audit sink; its own failures are logged, never recursive.
        self.episodic = EpisodicMemoryStore(self.settings, self.locks, self.validator)
        self.working = WorkingMemoryStore(
            self.settings, self.locks, self.validator, audit=self._audit
        )
        self.long_term = LongTermMemoryStore(
            self.settings, self.locks, self.validator, audit=self._audit
        )
        self.metrics = MetricsStore(self.settings, self.locks, audit=self._audit)
        self.migrations = MigrationManager(self.settings, self.locks, audit=self._audit)
        self.backup = BackupService(self.settings, self.locks, audit=self._audit)
        self.health = HealthService(self.settings, self.migrations)

        self.counter = TokenCounter(self.settings)
        self.retriever = MemoryRetriever(
            self.settings, self.working, self.long_term, self.episodic, counter=self.counter
        )
        self.context_builder = ContextBuilder(self.settings, counter=self.counter)
        self.compressor = MemoryCompressor(self.settings, counter=self.counter)
        self.formation = MemoryFormation(self.settings, self.long_term)

        self.provider_error: str | None = None
        if provider is not None:
            self.provider: BaseProvider = provider
        else:
            try:
                from nanomemory.providers import build_provider

                self.provider = build_provider(self.settings)
            except NanoMemoryError as exc:
                self.provider_error = exc.code.value
                # Keep the service observable/healthy-degraded instead of crashing at import
                # time; agent calls will fail with the configuration error.
                self.provider = MockProvider(model="unavailable")
        self.registry_tools: ToolRegistry = build_default_registry()
        self.agent = AgentLoop(
            self.settings,
            self.provider,
            self.registry_tools,
            self.retriever,
            self.context_builder,
            self.working,
            self.validator,
            self.metrics,
            self.counter,
            episodic=self.episodic,
        )
        self.recovery = RecoveryEngine(
            self.settings,
            self.locks,
            self.working,
            self.migrations,
            self.registry,
            audit=self._audit,
        )
        self.startup_report: StartupReport | None = None

    # ------------------------------------------------------------------ audit
    def _audit(self, *, event_type: str, session_id: str = "system", **fields: Any) -> None:
        """Append an audit episode; failures are logged and swallowed."""
        try:
            self.episodic.record_audit(
                event_type=event_type,
                session_id=session_id,
                summary=str(fields.get("summary") or event_type),
                related_memory_ids=fields.get("related_memory_ids"),
                outcome=str(fields.get("outcome") or "ok"),
                payload={key: value for key, value in fields.items() if key not in {"summary"}},
            )
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    # ------------------------------------------------------------------ lifecycle
    def initialize_storage(self) -> StorageReport:
        """Create every storage directory and the initial default session memory."""
        report = StorageReport(
            storage_root=str(self.settings.storage_root),
            default_session_id=self.settings.default_session_id,
        )
        for directory, label in (
            (self.settings.storage_root, "storage root"),
            (self.settings.working_dir, "working directory"),
            (self.settings.long_term_dir, "long-term directory"),
            (self.settings.episodic_dir, "episodic directory"),
            (self.settings.backups_dir, "backups directory"),
            (self.settings.metrics_dir, "metrics directory"),
            (self.settings.locks_dir, "locks directory"),
            (self.settings.health_dir, "health directory"),
            (self.settings.migrations_dir, "migrations directory"),
        ):
            if not directory.exists():
                ensure_dir(directory, root=self.settings.storage_root, label=label)
                report.created.append(label)
        self.long_term.ensure_storage()
        self.episodic.ensure_storage()
        self.metrics.ensure_storage()
        self.registry.ensure_default()
        self.working.ensure_session(self.settings.default_session_id)
        report.schema_versions = dict(self.migrations.current_versions())
        return report

    def startup(self) -> StartupReport:
        """Run the mandated startup order: maintenance recovery, then sessions."""
        report = self.recovery.startup()
        self.startup_report = report
        try:
            self.registry.ensure_default()
            if self.working.registry.exists(self.settings.default_session_id):
                directory = self.working.session_dir(self.settings.default_session_id)
                if not (directory / "memory.md").exists():
                    self.working.ensure_session(self.settings.default_session_id)
        except NanoMemoryError as exc:
            self.log.warning(
                "default_session_init_failed",
                extra={"event": "default_session_init_failed", "code": exc.code.value},
            )
        return report

    def is_ready(self) -> bool:
        report = self.startup_report
        return bool(report and report.ok and not report.maintenance_mode)

    # ------------------------------------------------------------------ sessions
    def create_session(self) -> SessionMeta:
        self.check_maintenance()
        meta = self.registry.create()
        self.working.ensure_session(meta.session_id)
        self._audit(event_type="session_created", session_id=meta.session_id)
        return meta

    def resolve_session(self, session_header: str | None) -> str:
        """Resolve the session id from the request header per the v1 rules."""
        if session_header:
            session_id = self.registry.validate_id(session_header)
            if not self.registry.exists(session_id):
                raise NanoMemoryError(
                    code=ErrorCode.SESSION_NOT_FOUND,
                    message="Session does not exist.",
                )
            return session_id
        if self.settings.is_single_session:
            self.registry.ensure_default()
            return self.settings.default_session_id
        raise NanoMemoryError(
            code=ErrorCode.SESSION_ID_REQUIRED,
            message="Header X-NanoMemory-Session-ID is required in multi-session mode.",
        )

    def list_sessions(self) -> list[str]:
        return self.registry.list_sessions()

    # ------------------------------------------------------------------ guards
    def check_maintenance(self) -> None:
        self.migrations.assert_not_in_maintenance()

    def _guard_session(self, session_id: str) -> None:
        self.registry.require(session_id)

    # ------------------------------------------------------------------ working memory
    def read_working_memory(self, session_id: str) -> WorkingMemorySnapshot:
        self.check_maintenance()
        return self.working.read(session_id)

    def propose_memory_update(
        self,
        session_id: str,
        proposal: Any,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Validate a proposal without writing (preview for the agent and API)."""
        self.check_maintenance()
        snapshot = self.working.read(session_id)
        current_version = snapshot.meta.memory_version
        if expected_version is not None and expected_version != current_version:
            raise NanoMemoryError(
                code=ErrorCode.VERSION_CONFLICT,
                message="Working memory version conflict.",
                details={"expected_version": expected_version, "current_version": current_version},
            )
        outcome = self.validator.validate_working_update(proposal, snapshot.document)
        return {
            "valid": True,
            "current_version": current_version,
            "document": outcome.value.model_dump(mode="json"),
            "secret_hits": outcome.secret_hits,
            "notes": outcome.notes,
        }

    def validate_memory_update(
        self, session_id: str, proposal: Any, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        return self.propose_memory_update(session_id, proposal, expected_version=expected_version)

    def apply_memory_update(
        self,
        session_id: str,
        proposal: Any,
        *,
        expected_version: int | None = None,
        operation: str = "api_update",
    ) -> UpdateResult:
        self.check_maintenance()
        self._guard_session(session_id)
        return self.working.update(
            session_id, proposal, expected_version=expected_version, operation=operation
        )

    def compress_working_memory(
        self, session_id: str, *, mode: str = "deterministic", apply: bool = False
    ) -> dict[str, Any]:
        """Compress (and optionally commit) a session's Working Memory."""
        self.check_maintenance()
        snapshot = self.working.read(session_id)
        result: CompressionResult = self.compressor.compress(snapshot.document, mode=mode)
        payload = result.as_dict()
        if apply and result.applied:
            from nanomemory.core.memory_models import MemoryUpdateProposal

            document = result.document
            proposal = MemoryUpdateProposal(
                current_goal=document.current_goal,
                status=document.status,
                relevant_context=document.relevant_context,
                completed=document.completed,
                in_progress=document.in_progress,
                blockers=document.blockers,
                next_action=document.next_action,
                allow_task_removal=False,
            )
            update = self.apply_memory_update(
                session_id,
                proposal,
                expected_version=snapshot.meta.memory_version,
                operation="compress_working",
            )
            payload["memory_version"] = update.snapshot.meta.memory_version
        return payload

    # ------------------------------------------------------------------ long term
    def add_long_term_memory(
        self, payload: dict[str, Any], *, session_id: str = "system"
    ) -> LongTermRecord:
        self.check_maintenance()
        return self.long_term.add(payload, session_id=session_id)

    def list_long_term_memory(self, **filters: Any) -> list[LongTermRecord]:
        self.check_maintenance()
        return self.long_term.list_records(**filters)

    def update_long_term_memory(
        self, memory_id: str, payload: dict[str, Any], *, if_version: int | None = None
    ) -> LongTermRecord:
        self.check_maintenance()
        return self.long_term.update(memory_id, payload, if_version=if_version)

    def archive_long_term_memory(
        self, memory_id: str, *, if_version: int | None = None
    ) -> LongTermRecord:
        self.check_maintenance()
        return self.long_term.archive(memory_id, if_version=if_version)

    def delete_long_term_memory(
        self, memory_id: str, *, permanent: bool = False, if_version: int | None = None
    ) -> bool:
        self.check_maintenance()
        return self.long_term.delete(memory_id, permanent=permanent, if_version=if_version)

    def retrieve_long_term_memory(self, query: str, *, limit: int = 20) -> list[LongTermRecord]:
        self.check_maintenance()
        return self.long_term.search(query, limit=limit)

    # ------------------------------------------------------------------ cognitive 2.0
    def remember(
        self,
        text: str,
        *,
        user_id: str = "local",
        source_kind: SourceKind = SourceKind.USER,
        confirm: bool | None = None,
        session_id: str = "system",
    ) -> dict[str, Any]:
        """Run the automatic memory formation pipeline over free text."""
        self.check_maintenance()
        return self.formation.form(
            text,
            user_id=user_id,
            source_kind=source_kind,
            source_ref=source_kind.value,
            session_id=session_id,
            confirm=confirm,
        )

    def recall(
        self,
        query: str,
        *,
        session_id: str,
        user_id: str | None = None,
        token_budget: int | None = None,
        memory_type: MemoryType | None = None,
    ) -> RetrievalResult:
        """Hybrid retrieval with relation expansion and a low-confidence fallback."""
        self.check_maintenance()
        result = self.retriever.retrieve(
            query,
            session_id=session_id,
            token_budget=token_budget,
            user_id=user_id,
            memory_type=memory_type,
        )
        recalled = [item.id for item in result.selected if item.kind == "long_term"]
        if recalled:
            self.long_term.touch_recalled(recalled)
        return result

    def confirm_memory(self, memory_id: str, *, session_id: str = "system") -> LongTermRecord:
        self.check_maintenance()
        return self.long_term.confirm(memory_id, session_id=session_id)

    def reject_memory(
        self, memory_id: str, *, reason: str = "rejected by operator", session_id: str = "system"
    ) -> LongTermRecord:
        self.check_maintenance()
        return self.long_term.reject(memory_id, reason=reason, session_id=session_id)

    def explain_memory(self, memory_id: str) -> dict[str, Any]:
        self.check_maintenance()
        return self.long_term.explain_data(memory_id)

    def run_lifecycle(self, *, dry_run: bool = True) -> dict[str, Any]:
        self.check_maintenance()
        return run_lifecycle(self.long_term, dry_run=dry_run)

    def retrieve_for_context(
        self, task: str, session_id: str, *, token_budget: int | None = None
    ) -> RetrievalResult:
        self.check_maintenance()
        return self.retriever.retrieve(task, session_id=session_id, token_budget=token_budget)

    # ------------------------------------------------------------------ episodic
    def append_episode(
        self,
        *,
        session_id: str,
        event_type: str,
        summary: str,
        outcome: str = "recorded",
        payload: dict[str, Any] | None = None,
        related_memory_ids: list[str] | None = None,
        correction_of: str | None = None,
    ) -> Episode:
        self.check_maintenance()
        return self.episodic.append(
            session_id=session_id,
            event_type=event_type,
            summary=summary,
            outcome=outcome,
            payload=payload,
            related_memory_ids=related_memory_ids,
            correction_of=correction_of,
        )

    def read_episodes(self, query: EpisodicQuery) -> EpisodicResult:
        self.check_maintenance()
        return self.episodic.read(query)

    def pending_quarantine(self) -> list[QuarantineRecord]:
        return self.episodic.pending_quarantine()

    def acknowledge_quarantine(
        self,
        quarantine_ids: list[str],
        *,
        reason: str,
        operator: str = "local-admin",
        request_id: str | None = None,
    ) -> list[QuarantineAck]:
        self.check_maintenance()
        return self.episodic.acknowledge(
            quarantine_ids, reason=reason, operator=operator, request_id=request_id
        )

    # ------------------------------------------------------------------ metrics
    def usage_records(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        limit: int | None = 200,
    ) -> tuple[list[UsageRecord], int]:
        return self.metrics.read(
            since=since, until=until, model=model, provider=provider, limit=limit
        )

    def metrics_summary(self) -> MetricsSummary:
        return self.metrics.summary()

    # ------------------------------------------------------------------ backup
    def create_backup(self, *, label: str = "manual", reason: str = "api") -> Any:
        self.check_maintenance()
        return self.backup.create(label=label, reason=reason)

    def list_backups(self) -> list[Any]:
        return self.backup.list_backups()

    def verify_backup(self, backup_id: str) -> dict[str, Any]:
        return self.backup.verify(backup_id)

    def restore_backup(
        self,
        backup_id: str,
        *,
        confirmation: str,
        force: bool = False,
    ) -> RestoreResult:
        self.check_maintenance()
        return self.backup.restore(backup_id, confirmation=confirmation, force=force)

    # ------------------------------------------------------------------ migrations
    def run_migrations(self) -> MigrationReport:
        self.check_maintenance()
        return self.migrations.migrate_all()

    # ------------------------------------------------------------------ agent
    async def run_agent(
        self,
        task: str,
        *,
        session_id: str,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        allow_write_memory: bool = False,
        request_id: str | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> AgentRunResult:
        self.check_maintenance()
        if self.provider_error is not None:
            raise NanoMemoryError(
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                message=(
                    "The configured provider is not usable; check NANOMEMORY_API_KEY and "
                    "NANOMEMORY_PROVIDER."
                ),
                details={"reason": self.provider_error},
            )
        return await self.agent.run(
            task,
            session_id=session_id,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
            allow_write_memory=allow_write_memory,
            request_id=request_id,
            cancel_token=cancel_token,
        )

    def tool_context(self, session_id: str, *, allow_write_memory: bool = False) -> ToolContext:
        return ToolContext(
            session_id=session_id,
            settings=self.settings,
            working=self.working,
            long_term=self.long_term,
            allow_write_memory=allow_write_memory,
        )

    # ------------------------------------------------------------------ health / stats
    def health_report(self) -> HealthReport:
        return self.health.check()

    def health_dict(self) -> dict[str, Any]:
        report = self.health.check()
        startup = self.startup_report.as_dict() if self.startup_report else None
        extra: dict[str, Any] = {"startup_recovery": startup}
        return health_payload(self.settings, report, extra=extra)

    def get_memory_statistics(self) -> dict[str, Any]:
        """Aggregate counters for the dashboard overview (no memory contents)."""
        sessions = self.registry.list_sessions()
        working_state: list[str] = []
        versions: list[int] = []
        for session_id in sessions:
            try:
                state = self.working.read_state(session_id).state.value
                working_state.append(state)
                snapshot = self.working.read(session_id)
                versions.append(snapshot.meta.memory_version)
            except NanoMemoryError:
                working_state.append("unreadable")
        episodic_stats = self.episodic.statistics()
        usage = self.metrics_summary()
        return {
            "sessions": len(sessions),
            "session_states": working_state,
            "working_versions": versions,
            "working_max_version": max(versions) if versions else 0,
            "long_term": self.long_term.statistics(),
            "episodic": episodic_stats,
            "metrics": usage.as_dict(),
            "backups": self.backup.statistics(),
            "migrations": self.migrations.status(),
            "budget": self.counter.budget_snapshot(),
            "provider": {
                "mode": self.settings.provider_mode,
                "provider": self.settings.provider,
                "model": self.provider.model_name,
                "error": self.provider_error,
            },
            "startup_recovery": self.startup_report.as_dict() if self.startup_report else None,
        }

    def export_snapshot(self) -> dict[str, Any]:
        """Full export of memory contents (data portability, spec §14)."""
        sessions: dict[str, Any] = {}
        for session_id in self.registry.list_sessions():
            try:
                snapshot = self.working.read(session_id)
                sessions[session_id] = {
                    "document": snapshot.document.model_dump(mode="json"),
                    "meta": snapshot.meta.model_dump(mode="json"),
                }
            except NanoMemoryError as exc:
                sessions[session_id] = {"error": exc.code.value}
        return {
            "schema_version": 1,
            "exported_at": datetime.now().astimezone().isoformat(),
            "sessions": sessions,
            "long_term": self.long_term.export_all(),
            "episodes": [
                view.episode.model_dump(mode="json")
                for view in self.read_episodes(EpisodicQuery(limit=1000)).events
            ],
        }

    async def aclose(self) -> None:
        await self.agent.aclose()


def build_manager(settings: Settings | None = None) -> MemoryManager:
    """Convenience factory used by scripts, API and benchmarks."""
    return MemoryManager(settings or get_settings())


DEFAULT_SESSION: str = DEFAULT_SESSION_ID

__all__ = ["DEFAULT_SESSION", "MemoryManager", "StorageReport", "build_manager"]
