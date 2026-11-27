"""Re-embed an existing v1 long-term store into the v2 vector store.

Copies, never moves: knowledge.json is opened read-only and left byte-identical.  The v1 record
id is preserved so the migration is idempotent -- running it twice reports "skipped" for rows
that already exist with the same content hash.

Usage:
    python scripts/migrate_long_term_to_vector.py --dry-run
    python scripts/migrate_long_term_to_vector.py --tenant default --user legacy-import
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.core.memory_models import MemoryStatus  # noqa: E402
from nanomemory.vector.schemas import (  # noqa: E402
    MemoryMetadata,
    TenantContext,
    VectorMemoryState,
    sha256_hex,
)
from nanomemory.vector.service import NewMemory, VectorMemoryService  # noqa: E402

# v1 status -> v2 lifecycle state.  Anything not active is migrated as archived so history is
# preserved without becoming retrievable.
_STATE_MAP = {
    MemoryStatus.ACTIVE: VectorMemoryState.ACTIVE,
    MemoryStatus.NEEDS_REVIEW: VectorMemoryState.ACTIVE,
    MemoryStatus.CONFLICT: VectorMemoryState.ARCHIVED,
    MemoryStatus.SUPERSEDED: VectorMemoryState.ARCHIVED,
    MemoryStatus.ARCHIVED: VectorMemoryState.ARCHIVED,
    MemoryStatus.PENDING_CONFIRMATION: VectorMemoryState.ARCHIVED,
}


async def migrate(
    *, dry_run: bool, tenant_id: str, user_id: str, source: Path | None = None
) -> dict[str, int]:
    settings = build_settings()
    knowledge = source or (settings.long_term_dir / "knowledge.json")
    if not knowledge.is_file():
        raise SystemExit(f"no v1 store at {knowledge}")
    payload = json.loads(knowledge.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    service = VectorMemoryService(settings)
    service.ensure_ready()
    context = TenantContext(tenant_id=tenant_id, user_id=user_id)
    stats = {"total": len(records), "created": 0, "skipped": 0, "archived": 0, "failed": 0}
    try:
        for record in records:
            memory_id = str(record.get("id") or "")
            content = str(record.get("content") or "").strip()
            if not memory_id or not content:
                stats["failed"] += 1
                continue
            try:
                existing = service.get(context, memory_id)
                if existing.content_sha256 == sha256_hex(content):
                    stats["skipped"] += 1
                    continue
            except Exception:  # noqa: BLE001 - not found is the normal case
                pass
            state = _STATE_MAP.get(
                MemoryStatus(record.get("status", "active")), VectorMemoryState.ACTIVE
            )
            metadata = MemoryMetadata(
                category=str(record.get("category") or "general"),
                tags=list(record.get("tags") or []),
                memory_type=record.get("memory_type") or "fact",
                importance=int(record.get("importance") or 3),
                source=str(record.get("source") or "v1-import"),
                source_type=record.get("source_type") or "import",
                confidence=record.get("confidence") or "medium",
                trust_level=record.get("trust_level") or "unknown",
                valid_from=record.get("valid_from"),
                valid_until=record.get("valid_until"),
                attributes={"v1_import": True, "v1_status": record.get("status")},
            )
            if dry_run:
                stats["created"] += 1
                if state is VectorMemoryState.ARCHIVED:
                    stats["archived"] += 1
                continue
            outcome = await service.remember(
                context,
                NewMemory(content=content, metadata=metadata, memory_id=memory_id),
            )
            if state is VectorMemoryState.ARCHIVED:
                service.archive(context, outcome.record.id)
                stats["archived"] += 1
            stats["created"] += 1
    finally:
        service.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tenant", default="local")
    parser.add_argument("--user", default="legacy-import")
    parser.add_argument("--source", type=Path, default=None)
    args = parser.parse_args()
    stats = asyncio.run(
        migrate(
            dry_run=args.dry_run,
            tenant_id=args.tenant,
            user_id=args.user,
            source=args.source,
        )
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
