"""Startup recovery orchestration (spec §28.1, §33.3, §36.3).

Fixed startup order:

1. Read the maintenance marker **before** any session recovery or API traffic.
2. If the marker exists: acquire maintenance + the full resource lock set, run migration
   recovery, and only clear the marker once the data is verifiably consistent.  Until then
   every memory/agent API answers ``503 maintenance_mode``.
3. Only then run per-session Working Memory recovery.  Each session recovery holds the
   global ``working`` lock plus its own ``working:<session>`` lock for the whole
   "read journal -> verify -> rollback/cleanup -> audit" section.
4. The service becomes ready for read/write only after every session recovery succeeded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, LockTimeoutError
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.migrations import MigrationManager, MigrationReport
from nanomemory.core.sessions import SessionRegistry
from nanomemory.core.working_memory import RecoveryOutcome, WorkingMemoryStore

AuditFn = Callable[..., None]


@dataclass(slots=True)
class StartupReport:
    """What startup recovery found and did (exposed by ``/health`` and the API)."""

    ok: bool = True
    maintenance_mode: bool = False
    degraded: bool = False
    migration: MigrationReport | None = None
    sessions: dict[str, list[str]] = field(default_factory=dict)
    failed_sessions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "maintenance_mode": self.maintenance_mode,
            "degraded": self.degraded,
            "migration": self.migration.status if self.migration else None,
            "sessions_recovered": len(self.sessions),
            "failed_sessions": self.failed_sessions,
            "reasons": self.reasons,
        }


class RecoveryEngine:
    """Runs migration recovery first, then session recovery, in the mandated order."""

    def __init__(
        self,
        settings: Settings,
        locks: LockManager,
        working: WorkingMemoryStore,
        migrations: MigrationManager,
        registry: SessionRegistry,
        *,
        audit: AuditFn | None = None,
    ) -> None:
        self.settings = settings
        self.locks = locks
        self.working = working
        self.migrations = migrations
        self.registry = registry
        self._audit_fn = audit
        self.log = get_logger("recovery")

    def _audit(self, event_type: str, *, session_id: str = "system", **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, session_id=session_id, **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    # ------------------------------------------------------------------ startup
    def startup(self) -> StartupReport:
        report = StartupReport()

        # ---- step 1/2: maintenance marker first ---------------------------
        if self.migrations.maintenance_active():
            report.maintenance_mode = True
            migration_report = self.migrations.recover_pending()
            report.migration = migration_report
            if migration_report.degraded or migration_report.status in {
                "recovery_required",
                "lock_timeout",
            }:
                report.ok = False
                report.degraded = True
                report.add_reason(f"migration_{migration_report.status}")
                self._audit(
                    "migration_recovery_required",
                    status=migration_report.status,
                )
                return report  # stay in maintenance mode; no session recovery
            self._audit(
                "migration_rollback_completed"
                if migration_report.status == "rolled_back"
                else "migration_completed",
                recovered=True,
            )
            report.maintenance_mode = self.migrations.maintenance_active()

        # ---- step 3: per-session recovery ---------------------------------
        for session_id in self.registry.list_sessions():
            try:
                outcome = self.working.recover(session_id)
            except LockTimeoutError as exc:
                report.ok = False
                report.degraded = True
                report.failed_sessions.append(session_id)
                report.add_reason(f"lock_timeout:{session_id}")
                self._audit(
                    "session_recovery_pending",
                    session_id=session_id,
                    reason=exc.code.value,
                )
                continue
            report.sessions[session_id] = list(outcome.actions)
            if outcome.failed:
                report.ok = False
                report.degraded = True
                report.failed_sessions.append(session_id)

        if report.failed_sessions:
            report.add_reason("session_recovery_incomplete")
        return report

    # ------------------------------------------------------------------ single session
    def recover_session(self, session_id: str) -> RecoveryOutcome:
        """Recover one session on demand (for example after an operator fixed a hash)."""
        if self.migrations.maintenance_active():
            from nanomemory.core.errors import NanoMemoryError

            raise NanoMemoryError(
                code=ErrorCode.MAINTENANCE_MODE,
                message="Service is in maintenance mode; session recovery is unavailable.",
            )
        return self.working.recover(session_id)


__all__ = ["RecoveryEngine", "StartupReport"]
