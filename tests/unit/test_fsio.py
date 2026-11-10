"""Unit tests for durable atomic IO and orphan temp handling (spec §22.3, §28.4)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from nanomemory.core import fsio
from nanomemory.core.errors import NanoMemoryError


def test_atomic_write_text_creates_file_without_temp(tmp_path: Path) -> None:
    target = tmp_path / "memory.md"
    result = fsio.atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert result.sha256 == fsio.sha256_bytes(b"hello")
    assert result.size == 5
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    fsio.atomic_write_text(target, "first")
    fsio.atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "meta.json"
    fsio.atomic_write_json(target, {"b": 1, "a": [1, 2]})
    assert fsio.read_json(target) == {"b": 1, "a": [1, 2]}


def test_temp_name_matches_mandated_pattern(tmp_path: Path) -> None:
    target = tmp_path / "memory.md"
    name = fsio.temp_name_for(target)
    assert name.startswith(".memory.md.nanomemory-tmp.")
    assert fsio.is_temp_name(name)
    assert not fsio.is_temp_name("memory.md.tmp")
    assert not fsio.is_temp_name(".memory.md.nanomemory-tmp.abc.1234")


def test_journal_temp_name_matches_pattern() -> None:
    name = fsio.journal_temp_name()
    assert fsio.is_journal_temp_name(name)
    assert not fsio.is_journal_temp_name("journal.prepared.tmp")
    assert not fsio.is_journal_temp_name("journal.json")


def test_failed_write_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "memory.md"
    target.write_text("original", encoding="utf-8")

    def boom(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        raise OSError(13, "replace denied")

    monkeypatch.setattr(fsio.os, "replace", boom)
    with pytest.raises(NanoMemoryError) as excinfo:
        fsio.atomic_write_text(target, "replacement")
    assert excinfo.value.code.value == "permission_denied"
    assert target.read_text(encoding="utf-8") == "original"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "memory.md"]
    assert leftovers == []


def test_corrupt_json_raises_corrupt_data(tmp_path: Path) -> None:
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        fsio.read_json(target)
    assert excinfo.value.code.value == "corrupt_data"


def test_read_json_respects_max_bytes(tmp_path: Path) -> None:
    target = tmp_path / "big.json"
    target.write_text(json.dumps({"a": "x" * 100}), encoding="utf-8")
    with pytest.raises(NanoMemoryError) as excinfo:
        fsio.read_json(target, max_bytes=10)
    assert excinfo.value.code.value == "file_too_large"


def _make_temp(directory: Path, target_name: str, age_seconds: float) -> Path:
    token = uuid.uuid4().hex
    name = f".{target_name}.nanomemory-tmp.{os.getpid()}.{token}"
    path = directory / name
    path.write_text("junk", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def test_orphan_temp_cleanup_allowlist(tmp_path: Path) -> None:
    """spec §28.4: only mandated, old, unreferenced temp files are listed for cleanup."""
    valid_old = _make_temp(tmp_path, "memory.md", 90_000)
    valid_young = _make_temp(tmp_path, "memory.md", 10)
    fake_name = tmp_path / "notes.tmp"
    fake_name.write_text("user data", encoding="utf-8")
    old = time.time() - 90_000
    os.utime(fake_name, (old, old))
    referenced = _make_temp(tmp_path, "memory.meta.json", 90_000)

    orphans = fsio.find_orphan_temps(
        tmp_path,
        max_age_seconds=86_400,
        is_referenced=lambda path: path == referenced,
    )
    assert orphans == [valid_old]
    assert valid_young.exists()
    assert fake_name.exists()
    assert referenced.exists()


def test_orphan_cleanup_skips_symlink(tmp_path: Path, make_link) -> None:
    real = _make_temp(tmp_path, "memory.md", 90_000)
    link = tmp_path / f".memory.md.nanomemory-tmp.{os.getpid()}.{uuid.uuid4().hex}"
    make_link(real, link)
    orphans = fsio.find_orphan_temps(tmp_path, max_age_seconds=0)
    assert link not in orphans


def test_fsync_dir_reports_platform_support(tmp_path: Path) -> None:
    supported = fsio.fsync_dir(tmp_path)
    if os.name == "nt":
        assert supported is False  # documented limitation
    else:  # pragma: no cover - POSIX only
        assert supported is True


def test_stat_signature_includes_hash(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("abc", encoding="utf-8")
    signature = fsio.stat_signature(target)
    assert signature["size"] == 3
    assert signature["sha256"] == fsio.sha256_bytes(b"abc")
