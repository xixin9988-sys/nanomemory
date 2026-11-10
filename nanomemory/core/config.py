"""Configuration for NanoMemory.

Precedence (spec §22.21): environment variables > ``.env`` > code defaults.  Every
variable uses the ``NANOMEMORY_`` prefix.  Relative paths are resolved against the
project root so the service behaves the same regardless of the current working directory
(spec §3).

Secrets: ``NANOMEMORY_API_KEY`` and ``NANOMEMORY_ADMIN_TOKEN`` are stored as
:class:`~pydantic.SecretStr` and are never logged, never serialised in health responses and
never written to storage.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

#: Fixed default session id used in single-session mode.  UUID4-shaped so the same
#: validator applies to default and server-created sessions.
DEFAULT_SESSION_ID = "11111111-1111-4111-8111-111111111111"

SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class Settings(BaseSettings):
    """Runtime settings loaded from ``NANOMEMORY_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="NANOMEMORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    # ------------------------------------------------------------------ product
    product_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"

    # ------------------------------------------------------------------ storage
    storage_root: Path = Field(default=PROJECT_ROOT / "storage")
    """Root of every persisted memory file.  Must stay writable and inside the project."""

    # ------------------------------------------------------------------ provider
    provider: Literal["mock", "openai_compatible"] = "mock"
    api_key: SecretStr | None = None
    base_url: str | None = None
    model: str = "mock-echo"
    provider_timeout_seconds: float = 60.0
    provider_max_retries: int = 2

    # ------------------------------------------------------------------ agent loop
    max_iterations: int = 5
    agent_timeout_seconds: float = 120.0
    agent_max_total_tokens: int = 20_000
    agent_max_tool_calls: int = 8
    agent_max_wall_time_seconds: float = 180.0
    agent_max_cost: float | None = None
    """Cost budget in ``cost_currency``.  ``None``/0 disables it (requires prices)."""

    cost_currency: str = "USD"

    # ------------------------------------------------------------------ budgets
    context_token_budget: int = 8000
    """Baseline token budget for *memory payload only* (spec §24.5).  Memory may use 80 %."""

    request_token_budget: int = 8000
    """Total request budget sent to the provider (spec §29.5).  Product default, not a
    provider context window."""

    context_budget_margin: float = 0.8

    # ------------------------------------------------------------------ locking
    lock_timeout_seconds: float = 5.0
    health_lock_timeout_seconds: float = 0.5

    # ------------------------------------------------------------------ retention
    working_snapshot_retention: int = 10
    orphan_tmp_max_age_seconds: int = 86_400
    backup_retention_count: int = 10
    backup_retention_daily: int = 7

    # ------------------------------------------------------------------ long term
    long_term_capacity_limit: int = 5000
    long_term_tag_overlap_threshold: int = 1

    # ------------------------------------------------------------------ metrics
    metrics_enabled: bool = True
    metrics_max_bytes: int = 10 * 1024 * 1024
    metrics_backup_count: int = 5

    # ------------------------------------------------------------------ health
    min_free_disk_mb: int = 200

    # ------------------------------------------------------------------ security
    admin_token: SecretStr | None = None
    secret_scan_mode: Literal["reject", "redact"] = "reject"
    secret_allowlist_regex: str = ""
    read_tool_max_file_bytes: int = 256 * 1024
    read_tool_allowed_extensions: str = ".md,.txt,.json,.jsonl,.py,.toml,.yaml,.yml,.csv"
    read_tool_allowed_root: Path | None = None
    request_max_bytes: int = 1_000_000

    # ------------------------------------------------------------------ sessions
    session_mode: Literal["single", "multi"] = "single"
    default_session_id: str = DEFAULT_SESSION_ID

    # ------------------------------------------------------------------ http
    worker_count: int = 1
    cors_allow_origins: str = ""
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    rate_limit_session_create_per_minute: int = 10
    rate_limit_agent_per_minute: int = 20
    rate_limit_backend: Literal["memory", "sqlite"] = "memory"

    # ------------------------------------------------------------------ compression
    compressor_llm_assisted: bool = False
    compressor_trigger_ratio: float = 0.9
    working_memory_max_chars: int = 12_000

    # ------------------------------------------------------------------ logging
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ------------------------------------------------------------------ pricing
    price_table_json: str = ""
    """JSON object: ``{"<model>": {"input_per_1k": 0.0, "output_per_1k": 0.0,
    "currency": "USD"}}``.  Empty means "no prices configured" -> cost is N/A."""

    # ------------------------------------------------------------------ validators
    @field_validator("storage_root", "read_tool_allowed_root", mode="after")
    @classmethod
    def _resolve_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @field_validator("context_budget_margin")
    @classmethod
    def _margin(cls, value: float) -> float:
        if not 0.1 <= value <= 1.0:
            raise ValueError("context_budget_margin must be between 0.1 and 1.0")
        return value

    @field_validator("secret_allowlist_regex")
    @classmethod
    def _allowlist_regex(cls, value: str) -> str:
        if value.strip():
            try:
                re.compile(value)
            except re.error as exc:  # pragma: no cover - defensive
                raise ValueError(f"invalid NANOMEMORY_SECRET_ALLOWLIST_REGEX: {exc}") from exc
        return value

    @field_validator("default_session_id")
    @classmethod
    def _session_id(cls, value: str) -> str:
        if not SESSION_ID_PATTERN.match(value):
            raise ValueError("default_session_id must be a UUID4-shaped string")
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> Settings:
        if self.context_token_budget < 1:
            raise ValueError("context_token_budget must be >= 1")
        if self.request_token_budget < 1:
            raise ValueError("request_token_budget must be >= 1")
        if self.working_snapshot_retention < 0:
            raise ValueError("working_snapshot_retention must be >= 0")
        if self.lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be > 0")
        if self.long_term_capacity_limit < 1:
            raise ValueError("long_term_capacity_limit must be >= 1")
        return self

    # ------------------------------------------------------------------ derived
    @property
    def working_dir(self) -> Path:
        return self.storage_root / "working"

    @property
    def long_term_dir(self) -> Path:
        return self.storage_root / "long_term"

    @property
    def episodic_dir(self) -> Path:
        return self.storage_root / "episodic"

    @property
    def backups_dir(self) -> Path:
        return self.storage_root / "backups"

    @property
    def metrics_dir(self) -> Path:
        return self.storage_root / "metrics"

    @property
    def locks_dir(self) -> Path:
        return self.storage_root / ".locks"

    @property
    def health_dir(self) -> Path:
        return self.storage_root / ".health"

    @property
    def migrations_dir(self) -> Path:
        return self.storage_root / "migrations"

    @property
    def maintenance_marker(self) -> Path:
        return self.storage_root / ".maintenance.json"

    @property
    def memory_token_limit(self) -> int:
        """Maximum tokens the retriever/context builder may spend on memory content."""
        return max(1, math.floor(self.context_token_budget * self.context_budget_margin))

    @property
    def allowed_extensions(self) -> frozenset[str]:
        return frozenset(
            ext.strip().lower()
            for ext in self.read_tool_allowed_extensions.split(",")
            if ext.strip()
        )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def provider_mode(self) -> Literal["mock", "real"]:
        return "mock" if self.provider == "mock" else "real"

    @property
    def is_single_session(self) -> bool:
        return self.session_mode == "single"

    def redacted_snapshot(self) -> dict[str, object]:
        """Non-sensitive configuration view for ``/health`` and benchmark metadata."""
        return {
            "product_version": self.product_version,
            "environment": self.environment,
            "provider": self.provider,
            "provider_mode": self.provider_mode,
            "model": self.model,
            "context_token_budget": self.context_token_budget,
            "memory_token_limit": self.memory_token_limit,
            "request_token_budget": self.request_token_budget,
            "context_budget_margin": self.context_budget_margin,
            "session_mode": self.session_mode,
            "storage_root": str(self.storage_root),
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "working_snapshot_retention": self.working_snapshot_retention,
            "long_term_capacity_limit": self.long_term_capacity_limit,
            "metrics_enabled": self.metrics_enabled,
            "admin_operations_enabled": self.admin_token is not None,
            "worker_count": self.worker_count,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (tests switch configuration between cases)."""
    get_settings.cache_clear()


def build_settings(**overrides: object) -> Settings:
    """Build a fresh :class:`Settings` with explicit overrides (used by tests and CLI).

    ``.env`` is deliberately ignored here: callers that want file-based configuration use
    :class:`Settings` directly.  This keeps tests hermetic.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_SESSION_ID",
    "PROJECT_ROOT",
    "SESSION_ID_PATTERN",
    "Settings",
    "build_settings",
    "get_settings",
    "reset_settings_cache",
]
