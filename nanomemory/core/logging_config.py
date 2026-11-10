"""Structured logging with mandatory secret redaction (spec §22.21, §29.8).

Every handler produced here is wrapped so that the *formatted output* passes through
:func:`nanomemory.core.redaction.redact`.  That single choke point covers the message,
structured fields, exception text and third-party loggers, and cannot be bypassed by
passing a secret in ``extra``.

Record fields (JSON format): ``timestamp, level, logger, request_id, event, message`` plus
any structured ``extra`` values.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from nanomemory.core.redaction import redact

REQUEST_ID_KEY = "request_id"
_request_id_var: ContextVar[str | None] = ContextVar("nanomemory_request_id", default=None)

#: Attributes that exist on every LogRecord; anything else was passed via ``extra``.
_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        REQUEST_ID_KEY,
        "event",
    }
)


def set_request_id(request_id: str | None) -> None:
    """Bind the current async/task context to ``request_id`` (used by the API middleware)."""
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id_var.get()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _RedactingMixin:
    """Formatter mixin that redacts the fully rendered output."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - logging API
        return redact(super().format(record))  # type: ignore[misc]


class JsonFormatter(_RedactingMixin, logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _timestamp(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, REQUEST_ID_KEY, None) or get_request_id(),
            "event": getattr(record, "event", None) or record.getMessage(),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        return redact(rendered)


class ConsoleFormatter(_RedactingMixin, logging.Formatter):
    """Human readable single-line format including the request id."""

    default_format = "%(asctime)s %(levelname)-8s %(name)s [rid=%(request_id)s] %(event)s"

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt or self.default_format, datefmt or "%Y-%m-%dT%H:%M:%S%z")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "event") or not record.event:  # type: ignore[attr-defined]
            record.event = record.getMessage()  # type: ignore[attr-defined]
        if not getattr(record, REQUEST_ID_KEY, None):
            record.request_id = get_request_id() or "-"  # type: ignore[attr-defined]
        formatted = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
            formatted = f"{formatted} {rendered}"
        return formatted


class RedactionFilter(logging.Filter):
    """Belt-and-braces filter: also redacts ``record.msg``/``record.args`` in place."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def build_handler(settings_format: str, level: int) -> logging.Handler:
    """Create a stream handler with the requested format and redaction."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter() if settings_format == "json" else ConsoleFormatter())
    handler.addFilter(RedactionFilter())
    return handler


def configure_logging(
    level: str = "INFO",
    log_format: str = "console",
    *,
    force: bool = False,
) -> logging.Logger:
    """Configure the root logger once; repeated calls are no-ops unless ``force``.

    Third-party loggers propagate to the root handler, so they inherit redaction.
    """
    root = logging.getLogger()
    numeric = getattr(logging, level.upper(), logging.INFO)
    if getattr(root, "_nanomemory_configured", False) and not force:
        return logging.getLogger("nanomemory")
    root.setLevel(numeric)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler(log_format, numeric))
    root._nanomemory_configured = True  # type: ignore[attr-defined]

    # uvicorn installs its own handlers; make them propagate to ours instead.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True
    return logging.getLogger("nanomemory")


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(name if name.startswith("nanomemory") else f"nanomemory.{name}")


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit a structured event: ``event`` is a stable machine readable name."""
    logger.log(level, message or event, extra={"event": event, **fields})


__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "RedactionFilter",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "log_event",
    "set_request_id",
]
