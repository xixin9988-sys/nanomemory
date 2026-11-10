"""Session creation endpoint (spec §24.3, §29.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from nanomemory.api.deps import ManagerDep, rate_limit_dependency
from nanomemory.api.schemas import SessionCreateResponse
from nanomemory.core.errors import ErrorCode, NanoMemoryError

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

_session_rate_limit = rate_limit_dependency(
    "session_create", "rate_limit_session_create_per_minute"
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_session_rate_limit)],
)
def create_session(manager: ManagerDep, request: Request) -> SessionCreateResponse:
    """Create a server-generated session.

    Client-supplied ids are not accepted: the body is ignored on purpose.  Session creation
    needs no admin token but is rate limited.
    """
    if manager.settings.is_single_session:
        raise NanoMemoryError(
            code=ErrorCode.INVALID_REQUEST,
            message=(
                "This instance runs in single-session mode; use the default session id and "
                "omit the session header. Set NANOMEMORY_SESSION_MODE=multi to enable "
                "multiple sessions."
            ),
            details={"session_mode": manager.settings.session_mode},
        )
    meta: Any = manager.create_session()
    return SessionCreateResponse(
        session_id=meta.session_id,
        created_at=meta.created_at,
        status=meta.status.value,
    )
