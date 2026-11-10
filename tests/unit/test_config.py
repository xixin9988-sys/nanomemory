"""Unit tests: configuration, package layout and initializer spelling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanomemory.core.config import DEFAULT_SESSION_ID, Settings, build_settings


def test_defaults_are_safe(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "storage")
    assert settings.provider == "mock"
    assert settings.provider_mode == "mock"
    assert settings.context_token_budget == 8000
    assert settings.request_token_budget == 8000
    assert settings.lock_timeout_seconds == 5.0
    assert settings.session_mode == "single"
    assert settings.default_session_id == DEFAULT_SESSION_ID
    assert settings.admin_token is None
    assert settings.worker_count == 1
    assert settings.rate_limit_enabled is True


def test_context_memory_limit_is_80_percent(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "s", context_token_budget=8000)
    assert settings.memory_token_limit == 6400


def test_context_memory_limit_floor_with_custom_budget(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "s", context_token_budget=1234)
    # floor(1234 * 0.8) == 987
    assert settings.memory_token_limit == 987


def test_context_budget_boundary_one(tmp_path: Path) -> None:
    settings = build_settings(storage_root=tmp_path / "s", context_token_budget=1)
    assert settings.memory_token_limit == 1  # never zero


def test_invalid_budget_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(storage_root=tmp_path / "s", context_token_budget=0)


def test_invalid_margin_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(storage_root=tmp_path / "s", context_budget_margin=0.0)


def test_invalid_allowlist_regex_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(storage_root=tmp_path / "s", secret_allowlist_regex="([")


def test_invalid_session_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(storage_root=tmp_path / "s", default_session_id="not-a-uuid")


def test_relative_storage_root_resolves_against_project_root() -> None:
    settings = build_settings(storage_root="storage")
    assert settings.storage_root.is_absolute()
    assert settings.storage_root.name == "storage"


def test_derived_directories(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    settings = build_settings(storage_root=root)
    assert settings.working_dir == root / "working"
    assert settings.long_term_dir == root / "long_term"
    assert settings.episodic_dir == root / "episodic"
    assert settings.backups_dir == root / "backups"
    assert settings.metrics_dir == root / "metrics"
    assert settings.locks_dir == root / ".locks"
    assert settings.health_dir == root / ".health"
    assert settings.maintenance_marker == root / ".maintenance.json"


def test_allowed_extensions_parsing(tmp_path: Path) -> None:
    settings = build_settings(
        storage_root=tmp_path / "s", read_tool_allowed_extensions=".md, .TXT ,,"
    )
    assert settings.allowed_extensions == frozenset({".md", ".txt"})


def test_allowed_origins_parsing(tmp_path: Path) -> None:
    settings = build_settings(
        storage_root=tmp_path / "s",
        cors_allow_origins="http://localhost:8501, http://127.0.0.1:8501",
    )
    assert settings.allowed_origins == [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]


def test_redacted_snapshot_has_no_secrets(tmp_path: Path) -> None:
    from pydantic import SecretStr

    settings = build_settings(
        storage_root=tmp_path / "s",
        provider="openai_compatible",
        api_key=SecretStr("sk-test-should-not-appear"),
        admin_token=SecretStr("admin-should-not-appear"),
    )
    snapshot = settings.redacted_snapshot()
    rendered = repr(snapshot)
    assert "sk-test" not in rendered
    assert "admin-should-not-appear" not in rendered
    assert snapshot["provider_mode"] == "real"
    assert snapshot["admin_operations_enabled"] is True


def test_project_package_layout_is_flat() -> None:
    """spec §26.3: flat layout with a single ``nanomemory`` application package."""
    package_root = Path(__file__).resolve().parents[2] / "nanomemory"
    assert package_root.is_dir()
    assert (package_root / "__init__.py").is_file()
    for forbidden in ("src",):
        assert not (package_root.parent / forbidden / "nanomemory").exists()


def test_package_initializer_names() -> None:
    """spec §33.1/§35.3: every package directory has a real ``__init__.py``."""
    project_root = Path(__file__).resolve().parents[2]
    package_root = project_root / "nanomemory"
    package_dirs = [
        path
        for path in package_root.rglob("*")
        if path.is_dir() and "__pycache__" not in path.parts and not path.name.startswith(".")
    ]
    assert package_dirs, "expected package sub-directories"
    for directory in package_dirs:
        assert (directory / "__init__.py").is_file(), f"missing __init__.py in {directory}"
        for wrong in ("init.py", "_init_.py", "__init.py", "_init__.py"):
            assert not (directory / wrong).exists(), f"misnamed initializer {wrong} in {directory}"


def test_package_import_smoke() -> None:
    """spec §24.1/§33.1: all public packages import cleanly."""
    import importlib

    for module in (
        "nanomemory",
        "nanomemory.core",
        "nanomemory.core.config",
        "nanomemory.core.errors",
        "nanomemory.core.fsio",
        "nanomemory.core.locks",
        "nanomemory.core.logging_config",
        "nanomemory.core.paths",
        "nanomemory.core.redaction",
    ):
        assert importlib.import_module(module) is not None


def test_settings_are_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nanomemory.core import config as config_module

    config_module.reset_settings_cache()
    monkeypatch.setenv("NANOMEMORY_STORAGE_ROOT", str(tmp_path / "from-env"))
    first = config_module.get_settings()
    second = config_module.get_settings()
    assert first is second
    assert first.storage_root == (tmp_path / "from-env").resolve()
    config_module.reset_settings_cache()
    assert sys.modules.get("nanomemory.core.config") is config_module


def test_settings_accept_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NANOMEMORY_CONTEXT_TOKEN_BUDGET", "4000")
    settings = Settings(_env_file=None, storage_root=tmp_path / "s")
    assert settings.context_token_budget == 4000
    assert settings.memory_token_limit == 3200
