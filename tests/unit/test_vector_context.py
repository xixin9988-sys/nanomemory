"""Signed tenant context: the only source of tenant/user scope."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from nanomemory.core.config import build_settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.vector.context import (
    TENANT_HEADER,
    USER_HEADER,
    context_from_headers,
    sign_headers,
)

SECRET = "unit-test-signing-secret"


def _signed_settings(tmp_path: Path):
    return build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        context_signing_key=SecretStr(SECRET),
    )


def test_unsigned_development_context_falls_back_to_local(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage", environment="test")
    context = context_from_headers({}, settings)
    assert context.tenant_id == "local"
    assert context.user_id == "local"
    assert context.signed is False


def test_signed_context_is_verified(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    headers = sign_headers(
        tenant_id="tenant-a", user_id="user-1", secret=SECRET, acl_tags=("team",)
    )
    context = context_from_headers(headers, settings)
    assert context.tenant_id == "tenant-a"
    assert context.user_id == "user-1"
    assert context.acl_tags == ("team",)
    assert context.signed is True


def test_unsigned_request_rejected_when_key_configured(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    with pytest.raises(NanoMemoryError) as excinfo:
        context_from_headers({}, settings)
    assert excinfo.value.code is ErrorCode.CONTEXT_REQUIRED
    assert excinfo.value.status == 401


def test_tampered_tenant_rejected(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    headers = sign_headers(tenant_id="tenant-a", user_id="user-1", secret=SECRET)
    headers[TENANT_HEADER] = "tenant-b"
    with pytest.raises(NanoMemoryError) as excinfo:
        context_from_headers(headers, settings)
    assert excinfo.value.code is ErrorCode.CONTEXT_SIGNATURE_INVALID


def test_tampered_user_rejected(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    headers = sign_headers(tenant_id="tenant-a", user_id="user-1", secret=SECRET)
    headers[USER_HEADER] = "user-2"
    with pytest.raises(NanoMemoryError) as excinfo:
        context_from_headers(headers, settings)
    assert excinfo.value.code is ErrorCode.CONTEXT_SIGNATURE_INVALID


def test_expired_signature_rejected(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    headers = sign_headers(
        tenant_id="tenant-a",
        user_id="user-1",
        secret=SECRET,
        timestamp=int(time.time()) - settings.context_signing_max_age_seconds - 5,
    )
    with pytest.raises(NanoMemoryError) as excinfo:
        context_from_headers(headers, settings)
    assert excinfo.value.code is ErrorCode.CONTEXT_SIGNATURE_INVALID


def test_wrong_secret_rejected(tmp_path: Path) -> None:
    settings = _signed_settings(tmp_path)
    headers = sign_headers(tenant_id="tenant-a", user_id="user-1", secret="another-secret")
    with pytest.raises(NanoMemoryError) as excinfo:
        context_from_headers(headers, settings)
    assert excinfo.value.code is ErrorCode.CONTEXT_SIGNATURE_INVALID


def test_production_requires_signing_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(storage_root=tmp_path / "storage", environment="production")
