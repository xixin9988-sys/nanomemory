"""Verified tenant context from signed gateway headers.

Threat being closed: a client that can call NanoMemory directly must not be able to choose
its own tenant_id/user_id.  Those values are absent from every request body and query
parameter; they exist only in an HMAC-signed header envelope produced by the gateway
(mem-guard) that shares NANOMEMORY_CONTEXT_SIGNING_KEY with this service.

Development mode (no signing key configured) trusts the headers so local runs stay simple,
but Settings refuses that mode when environment=production.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.schemas import TenantContext

TENANT_HEADER = "X-NM-Tenant"
USER_HEADER = "X-NM-User"
ROLES_HEADER = "X-NM-Roles"
ACL_HEADER = "X-NM-Acl"
TIMESTAMP_HEADER = "X-NM-Timestamp"
SIGNATURE_HEADER = "X-NM-Signature"

DEFAULT_TENANT = "local"
DEFAULT_USER = "local"


def canonical_payload(*, tenant: str, user: str, roles: str, acl: str, timestamp: str) -> bytes:
    """Exact bytes the gateway signs; header order never matters."""
    return "\n".join([tenant, user, roles, acl, timestamp]).encode("utf-8")


def sign_headers(
    *,
    tenant_id: str,
    user_id: str,
    secret: str,
    roles: tuple[str, ...] = (),
    acl_tags: tuple[str, ...] = (),
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the signed header set (used by tests, scripts and the mem-guard test-suite)."""
    roles_header = ",".join(roles)
    acl_header = ",".join(acl_tags)
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_payload(
            tenant=tenant_id, user=user_id, roles=roles_header, acl=acl_header, timestamp=stamp
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        TENANT_HEADER: tenant_id,
        USER_HEADER: user_id,
        ROLES_HEADER: roles_header,
        ACL_HEADER: acl_header,
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: digest,
    }


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def context_from_headers(headers: Mapping[str, str], settings: Settings) -> TenantContext:
    """Resolve the caller scope, verifying the envelope when a key is configured."""
    key = settings.context_signing_key
    request_id = headers.get("X-Request-ID") or headers.get("X-NanoMemory-Request-ID")

    if key is None:
        tenant = (headers.get(TENANT_HEADER) or DEFAULT_TENANT).strip()
        user = (headers.get(USER_HEADER) or DEFAULT_USER).strip()
        try:
            return TenantContext(
                tenant_id=tenant,
                user_id=user,
                roles=_split(headers.get(ROLES_HEADER)),
                acl_tags=_split(headers.get(ACL_HEADER)),
                request_id=request_id,
                signed=False,
            )
        except ValueError as exc:
            raise NanoMemoryError(
                code=ErrorCode.CONTEXT_REQUIRED,
                message="Invalid tenant context headers.",
                details={"reason": str(exc).splitlines()[0][:200]},
            ) from exc

    tenant = (headers.get(TENANT_HEADER) or "").strip()
    user = (headers.get(USER_HEADER) or "").strip()
    roles = headers.get(ROLES_HEADER) or ""
    acl = headers.get(ACL_HEADER) or ""
    stamp = (headers.get(TIMESTAMP_HEADER) or "").strip()
    signature = (headers.get(SIGNATURE_HEADER) or "").strip()
    if not (tenant and user and stamp and signature):
        raise NanoMemoryError(
            code=ErrorCode.CONTEXT_REQUIRED,
            message="A signed tenant context is required for this endpoint.",
            details={"headers": [TENANT_HEADER, USER_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER]},
        )
    try:
        sent_at = int(stamp)
    except ValueError as exc:
        raise NanoMemoryError(
            code=ErrorCode.CONTEXT_SIGNATURE_INVALID,
            message="Tenant context timestamp is malformed.",
        ) from exc
    age = abs(int(time.time()) - sent_at)
    if age > settings.context_signing_max_age_seconds:
        raise NanoMemoryError(
            code=ErrorCode.CONTEXT_SIGNATURE_INVALID,
            message="Tenant context signature has expired; re-sign the request.",
            details={
                "age_seconds": age,
                "max_age_seconds": settings.context_signing_max_age_seconds,
            },
        )
    expected = hmac.new(
        key.get_secret_value().encode("utf-8"),
        canonical_payload(tenant=tenant, user=user, roles=roles, acl=acl, timestamp=stamp),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise NanoMemoryError(
            code=ErrorCode.CONTEXT_SIGNATURE_INVALID,
            message="Tenant context signature is invalid.",
        )
    try:
        return TenantContext(
            tenant_id=tenant,
            user_id=user,
            roles=_split(roles),
            acl_tags=_split(acl),
            request_id=request_id,
            signed=True,
        )
    except ValueError as exc:
        raise NanoMemoryError(
            code=ErrorCode.CONTEXT_REQUIRED,
            message="Invalid tenant context headers.",
            details={"reason": str(exc).splitlines()[0][:200]},
        ) from exc


__all__ = [
    "ACL_HEADER",
    "DEFAULT_TENANT",
    "DEFAULT_USER",
    "ROLES_HEADER",
    "SIGNATURE_HEADER",
    "TENANT_HEADER",
    "TIMESTAMP_HEADER",
    "USER_HEADER",
    "canonical_payload",
    "context_from_headers",
    "sign_headers",
]
