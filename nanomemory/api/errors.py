"""Unified API error handling (spec §22.21).

Every error response has the same shape::

    {"code": "...", "message": "...", "details": {...}, "request_id": "..."}

No stack traces, file system paths or secrets are ever returned.  The request id is also sent
back in the ``X-NanoMemory-Request-ID`` response header and echoed in logs.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.logging_config import get_logger, get_request_id

REQUEST_ID_HEADER = "X-NanoMemory-Request-ID"


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    request_id = request_id or get_request_id() or "unknown"
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    if details:
        payload["details"] = details
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={REQUEST_ID_HEADER: request_id},
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers for typed, validation and unexpected errors."""
    log = get_logger("api.errors")

    @app.exception_handler(NanoMemoryError)
    async def _nano_memory_error(request: Request, exc: NanoMemoryError) -> JSONResponse:
        log.warning(
            "api_error",
            extra={
                "event": "api_error",
                "code": exc.code.value,
                "status": exc.status,
                "path": request.url.path,
            },
        )
        return error_response(
            code=exc.code.value,
            message=exc.message,
            status_code=exc.status,
            details=exc.details or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "type": error.get("type"),
                "msg": str(error.get("msg", ""))[:200],
            }
            for error in exc.errors()[:10]
        ]
        return error_response(
            code=ErrorCode.VALIDATION_FAILED.value,
            message="Request payload failed validation.",
            status_code=422,
            details={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.METHOD_NOT_ALLOWED,
            413: ErrorCode.PAYLOAD_TOO_LARGE,
        }
        code = mapping.get(exc.status_code, ErrorCode.INVALID_REQUEST)
        return error_response(
            code=code.value,
            message=str(exc.detail) if exc.detail else "Request failed.",
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", extra={"event": "unhandled_exception"})
        return error_response(
            code=ErrorCode.INTERNAL_ERROR.value,
            message="Internal server error.",
            status_code=500,
        )


__all__ = ["REQUEST_ID_HEADER", "error_response", "install_error_handlers"]
