"""Secret scanner and validator tests (spec §22.7, §29.7, §35.4)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from nanomemory.core.config import build_settings
from nanomemory.core.errors import SecretDetectedError, ValidationError
from nanomemory.core.memory_models import (
    Confidence,
    LongTermRecord,
    MemoryUpdateProposal,
    SourceType,
    TrustLevel,
)
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.secret_scanner import SecretScanner, is_jwt, shannon_entropy

FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
FAKE_ANTHROPIC = "sk-ant-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"
FAKE_GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
FAKE_GITHUB_PAT = "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
FAKE_HEX = hashlib.sha256(b"fixture").hexdigest()


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (FAKE_OPENAI, "openai_api_key"),
        (FAKE_ANTHROPIC, "anthropic_api_key"),
        (FAKE_GITHUB, "github_token"),
        (FAKE_GITHUB_PAT, "github_pat"),
        (FAKE_AWS, "aws_access_key"),
        (FAKE_PEM, "private_key_block"),
    ],
    # Explicit ids: pytest would otherwise put the fake key value into the node id, and node
    # ids are published in the JUnit artifact.
    ids=["openai", "anthropic", "github_token", "github_pat", "aws", "pem"],
)
def test_explicit_patterns_detected(text: str, pattern: str) -> None:
    scanner = SecretScanner()
    result = scanner.scan_text(text)
    assert result.found
    assert pattern in result.hit_types()


def test_hit_does_not_contain_the_secret() -> None:
    scanner = SecretScanner()
    result = scanner.scan_text(FAKE_OPENAI)
    rendered = repr(result.hits)
    assert "A1b2C3d4" not in rendered


def test_hex_hash_is_allowlisted() -> None:
    scanner = SecretScanner()
    assert not scanner.scan_text(FAKE_HEX).found


@pytest.mark.parametrize("length", [32, 40, 64])
def test_common_hash_lengths_allowlisted(length: int) -> None:
    token = ("0123456789abcdef" * 4)[:length]
    assert not SecretScanner().scan_text(token).found


def test_jwt_is_allowlisted_for_generic_rule_but_explicit_rules_still_apply() -> None:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "1234567890"}).encode()).decode()
    token = f"{header}.{payload}.{FAKE_HEX}"
    scanner = SecretScanner()
    assert is_jwt(token)
    assert not scanner.scan_text(token).found


def test_image_data_uri_payload_allowlisted() -> None:
    payload = base64.b64encode(b"\x89PNG" + bytes(range(256)) * 2).decode()
    text = f"data:image/png;base64,{payload}"
    assert not SecretScanner().scan_text(text).found


def test_random_high_entropy_string_detected() -> None:
    token = "Qm9vayBvZiBIaWdoIEVudHJvcHkgU3RyaW5ncyBUaGF0IExvb2sgUmFuZG9t"
    scanner = SecretScanner()
    result = scanner.scan_text(token)
    assert result.found
    assert "high_entropy_string" in result.hit_types()
    assert shannon_entropy(token) > 4.5


def test_long_natural_language_not_flagged() -> None:
    text = (
        "The quick brown fox jumps over the lazy dog while the team reviews the memory "
        "engine design document and discusses token budgets for the next release."
    )
    assert not SecretScanner().scan_text(text).found


def test_file_path_not_flagged() -> None:
    text = "C:/Users/example/OneDrive/Documents/projects/nanomemory/storage/working/memory.md"
    assert not SecretScanner().scan_text(text).found


def test_extra_allowlist_regex_applies_to_generic_only() -> None:
    token = "Qm9vayBvZiBIaWdoIEVudHJvcHkgU3RyaW5ncyBUaGF0IExvb2sgUmFuZG9t"
    scanner = SecretScanner(extra_allowlist_regex=r"^Qm9vay")
    assert not scanner.scan_text(token).found
    # Explicit credentials are never suppressed by the allowlist.
    assert scanner.scan_text(FAKE_OPENAI).found


def test_invalid_allowlist_regex_rejected_by_settings(tmp_path) -> None:
    # pydantic / pydantic-settings wrap the validator error in different exception classes
    # depending on the entry point, so assert on the message instead of the exact type.
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - the message is the contract here
        build_settings(storage_root=tmp_path / "s", secret_allowlist_regex="([")
    assert "secret_allowlist" in str(excinfo.value).lower()


def test_scan_value_walks_nested_structures() -> None:
    scanner = SecretScanner()
    result = scanner.scan_value({"a": [FAKE_OPENAI, {"b": FAKE_AWS}]})
    assert len(result.hits) >= 2
    assert any("payload" in hit.location for hit in result.hits)


def test_redact_text_replaces_hits() -> None:
    scanner = SecretScanner()
    cleaned, result = scanner.redact_text(f"key={FAKE_OPENAI} and {FAKE_AWS}")
    assert result.found
    assert "A1b2C3d4" not in cleaned
    assert "ABCDEFGHIJ" not in cleaned


# --------------------------------------------------------------------------- validator
def _settings(tmp_path, **overrides):
    return build_settings(storage_root=tmp_path / "storage", **overrides)


def test_validator_rejects_secret_by_default(tmp_path) -> None:
    settings = _settings(tmp_path)
    validator = MemoryValidator(settings)
    proposal = MemoryUpdateProposal(next_action=f"use {FAKE_OPENAI}")
    from nanomemory.core.memory_models import WorkingMemoryDocument

    with pytest.raises(SecretDetectedError):
        validator.validate_working_update(proposal, WorkingMemoryDocument())


def test_validator_redacts_when_configured(tmp_path) -> None:
    settings = _settings(tmp_path, secret_scan_mode="redact")
    validator = MemoryValidator(settings)
    from nanomemory.core.memory_models import WorkingMemoryDocument

    outcome = validator.validate_working_update(
        MemoryUpdateProposal(next_action=f"use {FAKE_OPENAI}"), WorkingMemoryDocument()
    )
    assert "A1b2C3d4" not in outcome.value.next_action
    assert outcome.secret_hits


def test_validator_clamps_model_confidence_and_importance(tmp_path) -> None:
    settings = _settings(tmp_path)
    validator = MemoryValidator(settings)
    outcome = validator.validate_long_term_record(
        {
            "id": "mem-0001",
            "content": "the sky is blue",
            "category": "facts",
            "source": "agent",
            "source_type": SourceType.MODEL.value,
            "confidence": Confidence.HIGH.value,
            "importance": 5,
            "tags": ["Weather"],
            "trust_level": TrustLevel.VERIFIED.value,
        }
    )
    record: LongTermRecord = outcome.value
    assert record.confidence is Confidence.LOW
    assert record.importance <= 3
    assert record.trust_level is not TrustLevel.VERIFIED
    assert record.tags == ["weather"]


def test_validator_rejects_oversized_working_memory(tmp_path) -> None:
    settings = _settings(tmp_path, working_memory_max_chars=200)
    validator = MemoryValidator(settings)
    from nanomemory.core.memory_models import WorkingMemoryDocument

    proposal = MemoryUpdateProposal(completed=["x" * 500])
    with pytest.raises(ValidationError):
        validator.validate_working_update(proposal, WorkingMemoryDocument())


def test_validator_rejects_too_many_items(tmp_path) -> None:
    settings = _settings(tmp_path)
    validator = MemoryValidator(settings)
    from nanomemory.core.memory_models import WorkingMemoryDocument

    proposal = MemoryUpdateProposal(in_progress=[f"task {i}" for i in range(200)])
    with pytest.raises(ValidationError):
        validator.validate_working_update(proposal, WorkingMemoryDocument())
