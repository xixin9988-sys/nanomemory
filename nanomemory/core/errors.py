"""Unified error taxonomy for NanoMemory.

Two things live here:

* :class:`ErrorCode` - the single source of truth for machine readable error codes used by
  the API envelope ``{"code", "message", "details", "request_id"}`` (§22.21).
* :class:`NanoMemoryError` - an exception carrying a code, an HTTP status and safe details.

Secrets must never be placed in ``message`` or ``details``; callers pass already-sanitised
text because the API layer echoes these fields to clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Machine readable error codes (stable API contract)."""

    # Request / validation
    VALIDATION_FAILED = "validation_failed"
    INVALID_REQUEST = "invalid_request"
    INVALID_JSON = "invalid_json"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"

    # Memory
    SECRET_DETECTED = "secret_detected"
    VERSION_CONFLICT = "version_conflict"
    MEMORY_NOT_FOUND = "memory_not_found"
    MEMORY_CAPACITY_REACHED = "memory_capacity_reached"
    CORRUPT_DATA = "corrupt_data"
    QUARANTINE_UNACKNOWLEDGED = "quarantine_unacknowledged"
    SCHEMA_UNSUPPORTED = "schema_unsupported"

    # Sessions
    SESSION_ID_REQUIRED = "session_id_required"
    SESSION_ID_INVALID = "session_id_invalid"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_RECOVERY_REQUIRED = "session_recovery_required"

    # Locking / transactions / maintenance
    LOCK_TIMEOUT = "lock_timeout"
    TXN_COMMIT_FAILED = "txn_commit_failed"
    MAINTENANCE_MODE = "maintenance_mode"
    SESSION_RECOVERY_PENDING = "session_recovery_pending"

    # Admin / destructive operations
    ADMIN_OPERATIONS_DISABLED = "admin_operations_disabled"
    ADMIN_AUTH_REQUIRED = "admin_auth_required"
    ADMIN_AUTH_INVALID = "admin_auth_invalid"
    CONFIRMATION_REQUIRED = "confirmation_required"

    # Filesystem safety
    PATH_NOT_ALLOWED = "path_not_allowed"
    SYMLINK_DETECTED = "symlink_detected"
    FILE_TOO_LARGE = "file_too_large"
    IO_ERROR = "io_error"
    DISK_FULL = "disk_full"
    PERMISSION_DENIED = "permission_denied"

    # Provider
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION = "provider_connection"
    PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
    PROVIDER_UNKNOWN = "provider_unknown"

    # Agent / budget
    BUDGET_EXCEEDED = "budget_exceeded"
    REQUEST_BUDGET_EXCEEDED = "request_budget_exceeded"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    AGENT_ITERATION_LIMIT = "agent_iteration_limit"
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_CANCELLED = "agent_cancelled"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_INVALID_ARGS = "tool_invalid_args"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"

    # Backup / restore / migration
    BACKUP_NOT_FOUND = "backup_not_found"
    BACKUP_INVALID = "backup_invalid"
    RESTORE_FAILED = "restore_failed"
    RESTORE_CONFIRMATION_REQUIRED = "restore_confirmation_required"
    MIGRATION_FAILED = "migration_failed"

    # Infrastructure
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    SERVICE_DEGRADED = "service_degraded"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


#: Default HTTP status for each error code.  Explicit mappings win over the 500 default.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.INVALID_JSON: 400,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.SECRET_DETECTED: 422,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.MEMORY_NOT_FOUND: 404,
    ErrorCode.MEMORY_CAPACITY_REACHED: 422,
    ErrorCode.CORRUPT_DATA: 500,
    ErrorCode.QUARANTINE_UNACKNOWLEDGED: 409,
    ErrorCode.SCHEMA_UNSUPPORTED: 422,
    ErrorCode.SESSION_ID_REQUIRED: 400,
    ErrorCode.SESSION_ID_INVALID: 400,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.SESSION_RECOVERY_REQUIRED: 503,
    ErrorCode.LOCK_TIMEOUT: 503,
    ErrorCode.TXN_COMMIT_FAILED: 500,
    ErrorCode.MAINTENANCE_MODE: 503,
    ErrorCode.SESSION_RECOVERY_PENDING: 503,
    ErrorCode.ADMIN_OPERATIONS_DISABLED: 503,
    ErrorCode.ADMIN_AUTH_REQUIRED: 401,
    ErrorCode.ADMIN_AUTH_INVALID: 401,
    ErrorCode.CONFIRMATION_REQUIRED: 400,
    ErrorCode.PATH_NOT_ALLOWED: 400,
    ErrorCode.SYMLINK_DETECTED: 400,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.IO_ERROR: 500,
    ErrorCode.DISK_FULL: 507,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.PROVIDER_NOT_CONFIGURED: 503,
    ErrorCode.PROVIDER_AUTH: 502,
    ErrorCode.PROVIDER_RATE_LIMIT: 502,
    ErrorCode.PROVIDER_TIMEOUT: 504,
    ErrorCode.PROVIDER_CONNECTION: 502,
    ErrorCode.PROVIDER_INVALID_RESPONSE: 502,
    ErrorCode.PROVIDER_UNKNOWN: 502,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.REQUEST_BUDGET_EXCEEDED: 413,
    ErrorCode.CONTEXT_BUDGET_EXCEEDED: 413,
    ErrorCode.AGENT_ITERATION_LIMIT: 200,
    ErrorCode.AGENT_TIMEOUT: 504,
    ErrorCode.AGENT_CANCELLED: 200,
    ErrorCode.TOOL_NOT_ALLOWED: 400,
    ErrorCode.TOOL_INVALID_ARGS: 422,
    ErrorCode.TOOL_TIMEOUT: 504,
    ErrorCode.TOOL_EXECUTION_FAILED: 500,
    ErrorCode.BACKUP_NOT_FOUND: 404,
    ErrorCode.BACKUP_INVALID: 422,
    ErrorCode.RESTORE_FAILED: 500,
    ErrorCode.RESTORE_CONFIRMATION_REQUIRED: 400,
    ErrorCode.MIGRATION_FAILED: 500,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.SERVICE_DEGRADED: 503,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


@dataclass(slots=True)
class NanoMemoryError(Exception):
    """Exception carrying a stable error code, HTTP status and safe details.

    ``details`` must never contain secrets, stack traces or absolute internal paths.
    """

    code: ErrorCode
    message: str
    http_status: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.http_status is None:
            self.http_status = HTTP_STATUS.get(self.code, 500)
        # Explicit call: ``@dataclass(slots=True)`` rebuilds the class, which breaks
        # zero-argument ``super()`` inside the original class body.
        Exception.__init__(self, self.message)

    @property
    def status(self) -> int:
        """HTTP status, never ``None`` after construction."""
        return int(self.http_status or 500)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the public error envelope (without request_id)."""
        payload: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class LockTimeoutError(NanoMemoryError):
    """Raised when a cross-process lock cannot be acquired within the timeout."""

    def __init__(
        self, resource: str, timeout: float, details: dict[str, Any] | None = None
    ) -> None:
        merged = {"resource": resource, "timeout_seconds": timeout}
        if details:
            merged.update(details)
        super().__init__(
            code=ErrorCode.LOCK_TIMEOUT,
            message=f"Timed out after {timeout:.1f}s waiting for lock resource '{resource}'.",
            details=merged,
        )


class VersionConflictError(NanoMemoryError):
    """Raised when ``expected_version`` / ``if_version`` does not match persisted state."""

    def __init__(self, expected: int, current: int, target: str = "working_memory") -> None:
        super().__init__(
            code=ErrorCode.VERSION_CONFLICT,
            message=f"{target} version conflict: expected {expected}, current {current}.",
            details={"target": target, "expected_version": expected, "current_version": current},
        )


class ValidationError(NanoMemoryError):
    """Raised when a memory proposal or payload fails validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=ErrorCode.VALIDATION_FAILED, message=message, details=details or {})


class SecretDetectedError(NanoMemoryError):
    """Raised when the secret scanner rejects content before persistence."""

    def __init__(self, hit_types: list[str], locations: list[str] | None = None) -> None:
        super().__init__(
            code=ErrorCode.SECRET_DETECTED,
            message="Content rejected: potential secret material detected.",
            details={"hit_types": hit_types, "locations": locations or []},
        )


class PathNotAllowedError(NanoMemoryError):
    """Raised when a path escapes the configured storage root or violates symlink rules."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=ErrorCode.PATH_NOT_ALLOWED, message=message, details=details or {})


class SymlinkDetectedError(NanoMemoryError):
    """Raised when a symlink is found in a controlled storage path."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=ErrorCode.SYMLINK_DETECTED, message=message, details=details or {})


class TxnCommitFailedError(NanoMemoryError):
    """Raised when a Working Memory transaction fails and cannot be reported as success."""

    def __init__(
        self,
        message: str,
        session_state: str = "active",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"session_state": session_state}
        if details:
            merged.update(details)
        super().__init__(
            code=ErrorCode.TXN_COMMIT_FAILED,
            message=message,
            http_status=500,
            details=merged,
        )


__all__ = [
    "ErrorCode",
    "HTTP_STATUS",
    "LockTimeoutError",
    "NanoMemoryError",
    "PathNotAllowedError",
    "SecretDetectedError",
    "SymlinkDetectedError",
    "TxnCommitFailedError",
    "ValidationError",
    "VersionConflictError",
]
