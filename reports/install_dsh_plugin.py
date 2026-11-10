"""Install nanomemory-dsh-plugin into a DSH desktop profile (local, no registry).

What it does (idempotent, with timestamped backups):

1. copies ``dsh-plugin/`` into ``<profile>/node_modules/nanomemory-dsh-plugin``,
2. adds the package to the profile ``package.json`` dependencies and ``dsh.profile.bundles``,
3. appends an ``mcp-nanomemory`` config entry to the profile ``cordis.patch.yml`` with the
   absolute Python / repo / storage paths of this checkout,
4. validates the edited JSON and prints what changed.

It deliberately does **not** run pnpm: the desktop app resolves bundles from
``node_modules`` and this keeps the running profile untouched apart from the three files above.

Usage::

    python reports/install_dsh_plugin.py --profile E:\\dsh-home\\profiles\\desktop
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "nanomemory-dsh-plugin"
BUNDLE_ID = "mcp-nanomemory"


def _backup(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def _install_package(profile: Path, source: Path) -> Path:
    destination = profile / "node_modules" / PACKAGE_NAME
    # DSH's profile ACL denies deleting child directories, so an existing install is
    # overwritten in place (dirs_exist_ok) instead of being removed and recreated.
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
    )
    return destination


def _register(profile: Path, python_path: str, project_root: str, storage_root: str) -> list[str]:
    changes: list[str] = []
    package_json = profile / "package.json"
    payload = json.loads(package_json.read_text(encoding="utf-8"))
    dependencies = payload.setdefault("dependencies", {})
    if PACKAGE_NAME not in dependencies:
        dependencies[PACKAGE_NAME] = "0.1.0"
        changes.append(f"added dependency {PACKAGE_NAME}")
    bundles = payload.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    if PACKAGE_NAME not in bundles:
        bundles.append(PACKAGE_NAME)
        changes.append(f"added bundle {PACKAGE_NAME}")
    if changes:
        _backup(package_json)
        package_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        json.loads(package_json.read_text(encoding="utf-8"))  # must stay valid

    patch_path = profile / "cordis.patch.yml"
    patch_text = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
    if BUNDLE_ID in patch_text:
        changes.append("cordis.patch.yml already configures mcp-nanomemory")
        return changes
    block = (
        "\n# NanoMemory DSH plugin: local install of this checkout.\n"
        f"- id: {BUNDLE_ID}\n"
        f"  name: {PACKAGE_NAME}\n"
        "  config:\n"
        f"    pythonPath: {python_path}\n"
        f"    projectRoot: {project_root}\n"
        f"    storageRoot: {storage_root}\n"
        "    failOnStartupError: false\n"
    )
    _backup(patch_path) if patch_path.is_file() else None
    patch_path.write_text(patch_text.rstrip("\n") + "\n" + block, encoding="utf-8")
    changes.append("appended mcp-nanomemory entry to cordis.patch.yml")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the NanoMemory DSH plugin locally")
    parser.add_argument("--profile", required=True, help="DSH profile directory")
    parser.add_argument("--plugin", default=str(PROJECT_ROOT / "dsh-plugin"))
    parser.add_argument(
        "--python",
        default=str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
        help="interpreter that can import nanomemory",
    )
    parser.add_argument("--storage", default=str(PROJECT_ROOT / "storage"))
    args = parser.parse_args(argv)

    profile = Path(args.profile).resolve()
    source = Path(args.plugin).resolve()
    python_path = str(Path(args.python).resolve()).replace("\\", "/")
    project_root = str(PROJECT_ROOT).replace("\\", "/")
    storage_root = str(Path(args.storage).resolve()).replace("\\", "/")

    if not (profile / "package.json").is_file():
        print(f"not a DSH profile (missing package.json): {profile}", file=sys.stderr)
        return 2
    if not (source / "package.json").is_file():
        print(f"plugin package not found: {source}", file=sys.stderr)
        return 2
    if not Path(python_path).is_file():
        print(f"python not found: {python_path}", file=sys.stderr)
        return 2

    destination = _install_package(profile, source)
    changes = _register(profile, python_path, project_root, storage_root)
    print(json.dumps({"profile": str(profile), "package": str(destination), "changes": changes}, indent=2))
    print("Restart DeepSeek Harness; tools appear as mcp__nanomemory__*.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
