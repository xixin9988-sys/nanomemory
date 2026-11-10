"""Run startup recovery and orphan-temp cleanup for every session.

Usage::

    python scripts/cleanup_orphans.py [--dry-run]

This is the supported operator action after a crash.  It holds the same locks as the API and
never deletes files that do not match NanoMemory's mandated temp naming rules.
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
    parser = argparse.ArgumentParser(description="Recover sessions and clean orphan temps")
    parser.add_argument("--dry-run", action="store_true", help="Only report what is found")
    args = parser.parse_args(argv)
    settings = build_settings()
    manager = MemoryManager(settings)
    report = manager.startup()
    payload = {
        "dry_run": args.dry_run,
        "startup": report.as_dict(),
        "sessions": report.sessions,
    }
    print(json.dumps(payload, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
