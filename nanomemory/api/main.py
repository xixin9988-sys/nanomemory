"""FastAPI application factory (spec §12, §22.9, §24.4, §28.2, §29.9).

Security posture for v1:

* binds to localhost by default (the CLI/scripts do this; the app itself makes no network
  assumptions), single worker, no anonymous destructive operations,
* destructive/admin endpoints require ``NANOMEMORY_ADMIN_TOKEN`` (disabled when unset),
* CORS is closed unless ``NANOMEMORY_CORS_ALLOW_ORIGINS`` lists explicit origins,
* every request gets a request id which is logged and returned,
* request bodies are size limited (Content-Length is mandatory for write methods),
* maintenance mode answers ``503 maintenance_mode`` for all memory/agent traffic.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from nanomemory import __version__
from nanomemory.api.deps import RATE_LIMIT_HEADER, RateLimiter
from nanomemory.api.errors import REQUEST_ID_HEADER, error_response, install_error_handlers
from nanomemory.api.routes import (
    admin,
    agent,
    cognitive,
    health,
    memory,
    metrics,
    sessions,
)
from nanomemory.core.config import Settings, get_settings
from nanomemory.core.errors import ErrorCode
from nanomemory.core.logging_config import configure_logging, get_logger, log_event, set_request_id
from nanomemory.core.memory_manager import MemoryManager

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
MAINTENANCE_EXEMPT = ("/health", "/api/v1/maintenance", "/docs", "/openapi.json", "/redoc")


@asynccontextmanager
async def lifespan(instance: FastAPI):  # noqa: ANN201
    """Initialise storage and run the mandated startup recovery before serving traffic."""
    manager: MemoryManager = instance.state.manager
    manager.initialize_storage()
    report = manager.startup()
    instance.state.startup_report = report
    log_event(
        get_logger("api"),
        "startup_complete",
        ok=report.ok,
        degraded=report.degraded,
        maintenance_mode=report.maintenance_mode,
        failed_sessions=len(report.failed_sessions),
    )
    try:
        yield
    finally:
        await manager.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    manager: MemoryManager | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(settings.log_level, settings.log_format)
    log = get_logger("api")

    app = FastAPI(
        title="NanoMemory API",
        description=(
            "Local single-user memory engine for AI agents. Memory content is treated as "
            "untrusted reference data."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.manager = manager or MemoryManager(settings, configure_logs=False)
    app.state.rate_limiter = RateLimiter(
        per_minute=settings.rate_limit_per_minute,
        enabled=settings.rate_limit_enabled,
        backend=settings.rate_limit_backend,
        worker_count=settings.worker_count,
    )
    if not app.state.rate_limiter.global_valid:  # spec §35.7: never claim a global limit
        log.warning(
            "rate_limit_not_global",
            extra={"event": "rate_limit_not_global", "worker_count": settings.worker_count},
        )

    install_error_handlers(app)

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            set_request_id(None)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        if request.url.path.startswith("/api/") or request.url.path == "/health":
            log.info(
                "request_completed",
                extra={
                    "event": "request_completed",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "request_id": request_id,
                },
            )
        return response

    @app.middleware("http")
    async def enforce_limits(request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        # 1. request body size (spec §12)
        if request.method in WRITE_METHODS:
            content_length = request.headers.get("content-length")
            if content_length is None or not content_length.isdigit():
                return error_response(
                    code=ErrorCode.PAYLOAD_TOO_LARGE.value,
                    message="A valid Content-Length header is required for write requests.",
                    status_code=411,
                    request_id=getattr(request.state, "request_id", None),
                )
            if int(content_length) > settings.request_max_bytes:
                return error_response(
                    code=ErrorCode.PAYLOAD_TOO_LARGE.value,
                    message="Request body exceeds the configured maximum size.",
                    status_code=413,
                    details={"max_bytes": settings.request_max_bytes},
                    request_id=getattr(request.state, "request_id", None),
                )
        # 2. maintenance mode (§28.2): memory/agent traffic is refused while migrating
        if path.startswith("/api/") and not path.startswith(MAINTENANCE_EXEMPT):
            marker = app.state.manager.migrations
            if marker.maintenance_active():
                return error_response(
                    code=ErrorCode.MAINTENANCE_MODE.value,
                    message="Service is in maintenance mode; memory operations are disabled.",
                    status_code=503,
                    details={"reason": "schema_migration"},
                    request_id=getattr(request.state, "request_id", None),
                )
        response = await call_next(request)
        if not app.state.rate_limiter.global_valid:
            response.headers["X-NanoMemory-RateLimit-Scope"] = "per-process"
        return response

    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=[
                "Content-Type",
                "Authorization",
                "X-NanoMemory-Session-ID",
                REQUEST_ID_HEADER,
            ],
        )
        log.info(
            "cors_enabled",
            extra={"event": "cors_enabled", "origins": len(settings.allowed_origins)},
        )

    # ------------------------------------------------------------------ routes
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(memory.router)
    app.include_router(cognitive.router)
    app.include_router(agent.router)
    app.include_router(metrics.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/api/v1/version", include_in_schema=False)
    def version_info() -> dict[str, Any]:
        return {
            "name": "NanoMemory",
            "version": __version__,
            "session_mode": settings.session_mode,
            "worker_count": settings.worker_count,
            "rate_limit_scope": "global" if app.state.rate_limiter.global_valid else "per-process",
            "retry_after_header": RATE_LIMIT_HEADER,
        }

    return app


app = create_app()


__all__ = ["app", "create_app"]
