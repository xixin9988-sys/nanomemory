"""Health check semantics (spec §25.7, §30.7).

The check is non-destructive to user data: the write probe happens in ``storage/.health``,
a controlled directory that must itself stay inside the storage root and must not be a
symlink or junction.  Any unexpected condition is reported as ``degraded`` with a
non-sensitive reason code; absolute paths, tokens and environment values are never returned.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.fsio import atomic_write_text, fsync_dir
from nanomemory.core.locks import probe_file_lock
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import SUPPORTED_SCHEMA_VERSIONS, utc_now_iso
from nanomemory.core.migrations import MigrationManager
from nanomemory.core.paths import ensure_dir, is_link_like

PROBE_CONTENT = "nanomemory-health-probe"


@dataclass(slots=True)
class HealthReport:
    """Structured health result returned by ``GET /health``."""

    status: str
    service: str
    version: str
    provider_mode: str
    provider: str
    model: str
    degraded: bool
    maintenance_mode: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    schema_version: dict[str, int] = field(default_factory=dict)
    checked_at: str = field(default_factory=utc_now_iso)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "service": self.service,
            "version": self.version,
            "provider": self.provider,
            "provider_mode": self.provider_mode,
            "model": self.model,
            "degraded": self.degraded,
            "maintenance_mode": self.maintenance_mode,
            "reasons": self.reasons,
            "checks": self.checks,
            "schema_version": self.schema_version,
            "context_token_budget": None,
            "checked_at": self.checked_at,
        }


class HealthService:
    """Runs storage, lock, disk and configuration checks."""

    def __init__(
        self,
        settings: Settings,
        migrations: MigrationManager,
        *,
        provider_error: str | None = None,
    ) -> None:
        self.settings = settings
        self.migrations = migrations
        self.provider_error = provider_error
        self.log = get_logger("health")

    def check(self, *, include_budget: dict[str, Any] | None = None) -> HealthReport:
        reasons: list[str] = []
        checks: dict[str, Any] = {}

        storage_ok, storage_detail = self._storage_check()
        checks["storage"] = storage_detail
        if not storage_ok:
            reasons.append("storage_unwritable")

        lock_ok, lock_detail = self._lock_check()
        checks["lock"] = lock_detail
        if not lock_ok:
            reasons.append("lock_unavailable")

        disk_ok, disk_detail = self._disk_check()
        checks["disk"] = disk_detail
        if not disk_ok:
            reasons.append("low_disk_space")

        maintenance = self.migrations.maintenance_active()
        if maintenance:
            reasons.append("maintenance_mode")

        if self.settings.provider == "openai_compatible" and self.settings.api_key is None:
            reasons.append("provider_missing_credentials")
        if self.provider_error:
            reasons.append("provider_initialisation_failed")
        if self.settings.worker_count > 1 and self.settings.rate_limit_backend == "memory":
            reasons.append("rate_limit_not_shared")

        checks["provider"] = {
            "mode": self.settings.provider_mode,
            "configured": self.settings.provider == "mock" or self.settings.api_key is not None,
            "error": self.provider_error,
        }
        checks["rate_limit"] = {
            "enabled": self.settings.rate_limit_enabled,
            "backend": self.settings.rate_limit_backend,
            "per_minute": self.settings.rate_limit_per_minute,
        }

        degraded = bool(reasons)
        report = HealthReport(
            status="degraded" if degraded else "ok",
            service="nanomemory",
            version=self.settings.product_version,
            provider_mode=self.settings.provider_mode,
            provider=self.settings.provider,
            model=self.settings.model,
            degraded=degraded,
            maintenance_mode=maintenance,
            reasons=reasons,
            checks=checks,
            schema_version=dict(SUPPORTED_SCHEMA_VERSIONS),
        )
        payload = report.as_dict()
        if include_budget:
            payload.update(include_budget)
        return report

    # ------------------------------------------------------------------ checks
    def _storage_check(self) -> tuple[bool, dict[str, Any]]:
        root = self.settings.storage_root
        health_dir = self.settings.health_dir
        started = time.perf_counter()
        try:
            if root.exists() and is_link_like(root):
                return False, {"ok": False, "reason": "storage_root_is_link"}
            if not root.is_dir():
                return False, {"ok": False, "reason": "storage_root_missing"}
            if health_dir.exists() and is_link_like(health_dir):
                return False, {"ok": False, "reason": "health_dir_is_link"}
            ensure_dir(health_dir, root=root, label="health directory")
            probe = health_dir / f"probe.{uuid.uuid4().hex}.tmp"
            atomic_write_text(probe, PROBE_CONTENT)
            content = probe.read_text(encoding="utf-8")
            removed = True
            try:
                probe.unlink()
            except OSError:
                removed = False
            fsync_dir(health_dir)
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            ok = content == PROBE_CONTENT
            return ok, {
                "ok": ok,
                "reason": None if ok else "probe_mismatch",
                "write_probe_ms": elapsed,
                "cleanup_ok": removed,
                "directory_fsync_supported": os.name != "nt",
            }
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return False, {"ok": False, "reason": type(exc).__name__.lower()}

    def _lock_check(self) -> tuple[bool, dict[str, Any]]:
        lock_path = self.settings.locks_dir / "health-probe.lock"
        started = time.perf_counter()
        try:
            ensure_dir(self.settings.locks_dir, root=self.settings.storage_root, label="locks")
            acquired = probe_file_lock(lock_path, timeout=self.settings.health_lock_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            return False, {"ok": False, "reason": type(exc).__name__.lower()}
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        return acquired, {
            "ok": acquired,
            "reason": None if acquired else "lock_timeout",
            "probe_ms": elapsed,
        }

    def _disk_check(self) -> tuple[bool, dict[str, Any]]:
        try:
            usage = shutil.disk_usage(self.settings.storage_root)
        except OSError:
            return True, {"ok": True, "reason": "unknown"}
        free_mb = usage.free // (1024 * 1024)
        ok = free_mb >= self.settings.min_free_disk_mb
        return ok, {
            "ok": ok,
            "free_mb": int(free_mb),
            "minimum_mb": self.settings.min_free_disk_mb,
        }


def health_payload(
    settings: Settings, report: HealthReport, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Merge the report with non-sensitive budget metadata for ``/health``."""
    payload = report.as_dict()
    payload.update(
        {
            "context_token_budget": settings.context_token_budget,
            "memory_token_limit": settings.memory_token_limit,
            "request_token_budget": settings.request_token_budget,
            "token_estimator": "heuristic-char4-v1",
            "usage_source_policy": "exact_when_provider_reports_usage_else_estimated",
            "session_mode": settings.session_mode,
            "worker_count": settings.worker_count,
        }
    )
    if extra:
        payload["extra"] = extra
    return payload


__all__ = ["HealthReport", "HealthService", "health_payload"]
