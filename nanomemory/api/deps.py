"""FastAPI dependencies: manager access, sessions, admin token and rate limiting.

Session rules (spec §24.3):

* single-session mode: a missing header means the fixed default session,
* multi-session mode: a missing header is a 400 ``session_id_required`` error — never an
  implicit new session,
* an unknown or malformed id is a typed 404/400, never mapped onto the default session.

Admin rules (spec §24.4): the token only comes from ``NANOMEMORY_ADMIN_TOKEN``, comparison is
constant-time, and when it is unset every destructive endpoint is disabled.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.memory_manager import MemoryManager

SESSION_HEADER = "X-NanoMemory-Session-ID"
RATE_LIMIT_HEADER = "Retry-After"


def get_manager(request: Request) -> MemoryManager:
    manager = getattr(request.app.state, "manager", None)
    if manager is None:  # pragma: no cover - app is always created with a manager
        raise NanoMemoryError(
            code=ErrorCode.SERVICE_UNAVAILABLE, message="Memory manager is not initialised."
        )
    return manager


def get_settings_dep(request: Request) -> Settings:
    return get_manager(request).settings


ManagerDep = Annotated[MemoryManager, Depends(get_manager)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_session_id(request: Request, manager: ManagerDep) -> str:
    header = request.headers.get(SESSION_HEADER)
    return manager.resolve_session(header)


SessionDep = Annotated[str, Depends(get_session_id)]


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


RequestIdDep = Annotated[str, Depends(get_request_id)]


def require_admin(request: Request, manager: ManagerDep) -> None:
    """Enforce the admin bearer token for destructive operations."""
    settings = manager.settings
    configured = settings.admin_token
    if configured is None or not configured.get_secret_value().strip():
        raise NanoMemoryError(
            code=ErrorCode.ADMIN_OPERATIONS_DISABLED,
            message=(
                "Destructive admin operations are disabled because NANOMEMORY_ADMIN_TOKEN "
                "is not set."
            ),
        )
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if not token or scheme.lower() != "bearer":
        raise NanoMemoryError(
            code=ErrorCode.ADMIN_AUTH_REQUIRED,
            message="Admin bearer token is required for this operation.",
        )
    if not hmac.compare_digest(token.strip(), configured.get_secret_value()):
        raise NanoMemoryError(
            code=ErrorCode.ADMIN_AUTH_INVALID,
            message="Admin token is invalid.",
        )


AdminDep = Annotated[None, Depends(require_admin)]


# --------------------------------------------------------------------------- rate limiting
@dataclass(slots=True)
class RateLimiter:
    """Fixed-window per-client limiter.

    The backend is in-memory, which is only correct for a single-process deployment.  When
    more than one worker is configured the limiter reports ``global_valid=False`` and the
    health endpoint degrades, instead of claiming a global limit (spec §35.7).
    """

    per_minute: int = 60
    enabled: bool = True
    backend: str = "memory"
    worker_count: int = 1
    window_seconds: float = 60.0
    _buckets: dict[tuple[str, str], deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    @property
    def global_valid(self) -> bool:
        return self.worker_count <= 1 or self.backend != "memory"

    def hit(self, *, client: str, bucket: str, limit: int | None = None) -> tuple[bool, float]:
        """Register a request; return ``(allowed, retry_after_seconds)``."""
        if not self.enabled:
            return True, 0.0
        effective = limit or self.per_minute
        if effective <= 0:
            return True, 0.0
        key = (client, bucket)
        now = time.monotonic()
        entries = self._buckets[key]
        while entries and now - entries[0] > self.window_seconds:
            entries.popleft()
        if len(entries) >= effective:
            retry_after = max(0.0, self.window_seconds - (now - entries[0]))
            return False, round(retry_after, 2)
        entries.append(now)
        return True, 0.0

    def reset(self) -> None:
        self._buckets.clear()


def client_identity(request: Request) -> str:
    """Identify the caller without trusting arbitrary proxy headers."""
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(bucket: str, limit_attr: str):
    """Build a dependency enforcing one named bucket."""

    def dependency(request: Request, manager: ManagerDep) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        settings = manager.settings
        limit = int(getattr(settings, limit_attr))
        allowed, retry_after = limiter.hit(
            client=client_identity(request), bucket=bucket, limit=limit
        )
        if not allowed:
            raise NanoMemoryError(
                code=ErrorCode.RATE_LIMIT_EXCEEDED,
                message="Rate limit exceeded for this endpoint.",
                details={"bucket": bucket, "retry_after_seconds": retry_after},
                http_status=429,
            )

    return dependency


__all__ = [
    "AdminDep",
    "ManagerDep",
    "RATE_LIMIT_HEADER",
    "RateLimiter",
    "RequestIdDep",
    "SESSION_HEADER",
    "SessionDep",
    "SettingsDep",
    "client_identity",
    "get_manager",
    "get_request_id",
    "get_session_id",
    "rate_limit_dependency",
    "require_admin",
]
