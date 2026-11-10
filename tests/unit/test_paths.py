"""Unit tests for path containment and symlink policy (spec §22.8, §24.3)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanomemory.core import paths
from nanomemory.core.errors import PathNotAllowedError, SymlinkDetectedError


def test_ensure_within_accepts_child(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    child = root / "working" / "memory.md"
    assert paths.ensure_within(root, child, label="memory") == child.resolve()


def test_ensure_within_accepts_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    assert paths.ensure_within(root, root) == root.resolve()


@pytest.mark.parametrize(
    "candidate",
    ["../escape.md", "..\\escape.md", "../../etc/passwd", "working/../../escape.md"],
)
def test_ensure_within_rejects_traversal(tmp_path: Path, candidate: str) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    with pytest.raises(PathNotAllowedError):
        paths.ensure_within(root, root / candidate)


def test_ensure_within_rejects_absolute_escape(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathNotAllowedError):
        paths.ensure_within(root, outside)


def test_windows_case_and_backslash_normalisation(tmp_path: Path) -> None:
    root = tmp_path / "Storage"
    root.mkdir()
    child = root / "Working" / "MEMORY.md"
    child.parent.mkdir()
    assert paths.ensure_within(root, child) == child.resolve()
    if os.name == "nt":
        upper = Path(str(child).upper())
        assert paths.is_within(root.resolve(), upper.resolve())


def test_safe_component_rejects_bad_names() -> None:
    for bad in ("", ".", "..", "a/b", "a\\b", "C:", "with space", "x" * 200):
        with pytest.raises(PathNotAllowedError):
            paths.safe_component(bad)


def test_safe_component_accepts_uuid() -> None:
    assert paths.safe_component("123e4567-e89b-42d3-a456-426614174000")


def test_symlink_component_detected(tmp_path: Path, make_link) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    link = root / "linked"
    kind = make_link(target_dir, link, directory=True)
    assert paths.find_symlink_component(root, link / "file.txt") == link
    with pytest.raises(SymlinkDetectedError):
        paths.ensure_no_symlink(root, link / "file.txt")
    assert paths.is_link_like(link), kind


def test_junction_is_rejected_like_symlink(tmp_path: Path, make_link) -> None:
    """NTFS junctions are reparse points: ``is_symlink()`` is False but they must be blocked."""
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / "junction"
    make_link(outside, junction, directory=True)
    with pytest.raises(SymlinkDetectedError):
        paths.ensure_no_symlink(root, junction / "memory.md")


def test_data_file_symlink_rejected(tmp_path: Path, make_link) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    real = tmp_path / "secret.txt"
    real.write_text("do not follow", encoding="utf-8")
    link = root / "memory.md"
    make_link(real, link)
    with pytest.raises(SymlinkDetectedError):
        paths.ensure_no_symlink(root, link)


def test_join_safe_builds_contained_path(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    assert paths.join_safe(root, "working", "sessions") == (root / "working" / "sessions").resolve()
    with pytest.raises(PathNotAllowedError):
        paths.join_safe(root, "working", "..")


def test_relative_posix(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    nested = root / "working" / "a" / "memory.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("x", encoding="utf-8")
    assert paths.relative_posix(nested, root) == "working/a/memory.md"


def test_human_size() -> None:
    assert paths.human_size(12) == "12 B"
    assert paths.human_size(2048).endswith("KiB")
