"""Backup/restore CLI for the NanoMemory vector database.

    python scripts/vector_backup.py backup  --database storage/vector/vectors.db --out backups/
    python scripts/vector_backup.py verify  --backup backups/vectors-....db
    python scripts/vector_backup.py restore --backup backups/vectors-....db \
        --database storage/vector/vectors.db --force

Stop the service before restore: SQLite cannot safely replace a file that a writer has open.
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
from nanomemory.vector.backup import (  # noqa: E402
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _default_database() -> Path:
    return build_settings().vector_database_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="create a consistent backup + manifest")
    backup.add_argument("--database", type=Path, default=None)
    backup.add_argument("--out", type=Path, required=True)

    verify = sub.add_parser("verify", help="verify hash, integrity and schema")
    verify.add_argument("--backup", type=Path, required=True)

    restore = sub.add_parser("restore", help="restore a verified backup")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", type=Path, default=None)
    restore.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = create_backup(args.database or _default_database(), args.out)
            print(json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "verify":
            result = verify_backup(args.backup)
            print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
            return 0 if result.valid else 2
        if args.command == "restore":
            result = restore_backup(
                args.backup, args.database or _default_database(), force=args.force
            )
            print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
            return 0
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
