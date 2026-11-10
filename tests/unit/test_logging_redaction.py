"""Unit tests for secret redaction and structured logging (spec §29.8)."""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest

from nanomemory.core import logging_config
from nanomemory.core.redaction import REDACTED, redact, redact_mapping


@pytest.fixture()
def log_stream() -> StringIO:
    return StringIO()


def _build_logger(stream: StringIO, fmt: str = "json") -> logging.Logger:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging_config.JsonFormatter() if fmt == "json" else logging_config.ConsoleFormatter()
    )
    handler.addFilter(logging_config.RedactionFilter())
    logger = logging.getLogger(f"nanomemory.test.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
FAKE_ANTHROPIC = "sk-ant-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"
FAKE_GITHUB = "ghp_" + "a" * 30
FAKE_GITHUB_PAT = "github_pat_" + "b" * 30
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_BEARER = "Authorization: Bearer abcdef0123456789abcdef"
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA1234567890abcdef\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    "sample",
    [FAKE_OPENAI, FAKE_ANTHROPIC, FAKE_GITHUB, FAKE_GITHUB_PAT, FAKE_AWS, FAKE_PEM],
    # Explicit ids keep the fake key values out of node ids (they appear in JUnit artifacts).
    ids=["openai", "anthropic", "github_token", "github_pat", "aws", "pem"],
)
def test_redact_removes_secret_patterns(sample: str) -> None:
    cleaned = redact(f"value={sample} tail")
    assert REDACTED in cleaned
    for secret_piece in sample.split("\n"):
        if len(secret_piece) > 20 and "BEGIN" not in secret_piece:
            assert secret_piece not in cleaned


def test_redact_authorization_header_preserves_header_name() -> None:
    cleaned = redact(FAKE_BEARER)
    assert cleaned.startswith("Authorization:")
    assert "abcdef0123456789" not in cleaned


def test_redact_is_idempotent() -> None:
    once = redact(f"key={FAKE_OPENAI}")
    assert redact(once) == once


def test_redact_mapping_handles_nested() -> None:
    cleaned = redact_mapping({"a": FAKE_OPENAI, "b": {"c": [FAKE_AWS, 3]}, "d": 1})
    assert cleaned["d"] == 1
    assert REDACTED in cleaned["a"]
    assert REDACTED in cleaned["b"]["c"][0]


def test_json_logger_redacts_message_and_extras(log_stream: StringIO) -> None:
    logger = _build_logger(log_stream)
    logger.info("calling provider with %s", FAKE_OPENAI, extra={"api_key": FAKE_AWS})
    output = log_stream.getvalue()
    payload = json.loads(output.strip())
    assert payload["level"] == "INFO"
    assert payload["logger"].startswith("nanomemory.test")
    assert payload["event"]
    assert payload["api_key"] == REDACTED
    assert FAKE_OPENAI not in output
    assert FAKE_AWS not in output


def test_json_logger_redacts_exception_text(log_stream: StringIO) -> None:
    logger = _build_logger(log_stream)
    try:
        raise RuntimeError(f"auth failed for {FAKE_BEARER}")
    except RuntimeError:
        logger.exception("provider call failed")
    output = log_stream.getvalue()
    assert "abcdef0123456789" not in output
    payload = json.loads(output.strip())
    assert REDACTED in payload["exception"]


def test_console_logger_redacts_and_keeps_request_id(log_stream: StringIO) -> None:
    logger = _build_logger(log_stream, fmt="console")
    logging_config.set_request_id("req-123")
    try:
        logger.warning("token %s", FAKE_OPENAI, extra={"event": "provider_call"})
    finally:
        logging_config.set_request_id(None)
    output = log_stream.getvalue()
    assert "req-123" in output
    assert "provider_call" in output
    assert FAKE_OPENAI not in output


def test_redaction_filter_mutates_record() -> None:
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key %s",
        args=(FAKE_OPENAI,),
        exc_info=None,
    )
    logging_config.RedactionFilter().filter(record)
    assert FAKE_OPENAI not in record.getMessage()


def test_configure_logging_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging_config.configure_logging("INFO", "json", force=True)
    assert logger.name == "nanomemory"
    root = logging.getLogger()
    assert len(root.handlers) == 1
    logging_config.configure_logging("INFO", "json")
    assert len(root.handlers) == 1
    for handler in root.handlers:
        root.removeHandler(handler)
    root._nanomemory_configured = False  # type: ignore[attr-defined]


def test_log_event_emits_structured_event(log_stream: StringIO) -> None:
    logger = _build_logger(log_stream)
    logging_config.log_event(logger, "memory_updated", session_id="s1", version=3)
    payload = json.loads(log_stream.getvalue().strip())
    assert payload["event"] == "memory_updated"
    assert payload["session_id"] == "s1"
    assert payload["version"] == 3
