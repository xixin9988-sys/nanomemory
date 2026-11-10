"""Initialise the NanoMemory storage tree.

Usage::

    python scripts/init_storage.py [--root PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.core.memory_manager import MemoryManager  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise NanoMemory storage")
    parser.add_argument(
        "--root",
        default="",
        help="Storage root override (default: NANOMEMORY_STORAGE_ROOT or ./storage)",
    )
    args = parser.parse_args(argv)
    overrides: dict[str, object] = {}
    if args.root:
        overrides["storage_root"] = Path(args.root)
    settings = build_settings(**overrides)
    manager = MemoryManager(settings)
    report = manager.initialize_storage()
    startup = manager.startup()
    print(
        json.dumps(
            {
                "storage_root": report.storage_root,
                "default_session_id": report.default_session_id,
                "created": report.created,
                "schema_versions": report.schema_versions,
                "startup": startup.as_dict(),
            },
            indent=2,
        )
    )
    return 0 if startup.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
