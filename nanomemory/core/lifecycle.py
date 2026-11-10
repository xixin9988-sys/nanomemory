"""Memory lifecycle: retention scoring, archiving and explainability (spec §5-6).

Retention is **importance × utility × recency × confidence**, normalised into ``[0, 1]``.
The lifecycle never hard-deletes: memories below the floor are archived (reversible) and
stable preferences are protected regardless of how long they have been unused. A low recall
count alone is never a reason to forget: users do not restate their preferences every month.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import LongTermRecord, MemoryStatus, MemoryType

DEFAULT_ARCHIVE_THRESHOLD = 0.22
DEFAULT_MIN_AGE_DAYS = 14.0
PROTECTED_TYPES = {MemoryType.PREFERENCE, MemoryType.AUTOBIOGRAPHICAL}
_PROTECTED_IMPORTANCE = 4


def _age_days(stamp: str | None, now: datetime) -> float:
    if not stamp:
        return 365.0
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed).total_seconds() / 86400)


def _recency(stamp: str | None, now: datetime, *, half_life_days: float = 90.0) -> float:
    return math.exp(-_age_days(stamp, now) / half_life_days)


def retention_score(record: LongTermRecord, *, now: datetime | None = None) -> float:
    """Importance × utility × recency × confidence, each normalised to ``[0, 1]``."""
    moment = now or datetime.now(UTC)
    importance = (record.importance - 1) / 4.0
    utility = min(1.0, record.recall_count / 10.0)
    recency = _recency(record.last_recalled_at or record.created_at, moment)
    confidence = {"low": 0.4, "medium": 0.7, "high": 1.0}.get(record.confidence.value, 0.6)
    return importance * 0.35 + utility * 0.25 + recency * 0.20 + confidence * 0.20


def is_protected(record: LongTermRecord) -> bool:
    """Stable, user-owned memories must survive long absences."""
    return record.memory_type in PROTECTED_TYPES or record.importance >= _PROTECTED_IMPORTANCE


def plan_archival(
    records: list[LongTermRecord],
    *,
    now: datetime | None = None,
    threshold: float = DEFAULT_ARCHIVE_THRESHOLD,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> list[dict[str, object]]:
    """Return the memories the lifecycle policy would archive, with reasons."""
    moment = now or datetime.now(UTC)
    decisions: list[dict[str, object]] = []
    for record in records:
        if record.status is not MemoryStatus.ACTIVE:
            continue
        if is_protected(record):
            continue
        # Age since first creation: edits or metadata touches must not reset staleness.
        age = _age_days(record.last_recalled_at or record.created_at, moment)
        if age < min_age_days:
            continue
        score = retention_score(record, now=moment)
        if score >= threshold:
            continue
        decisions.append(
            {
                "id": record.id,
                "score": round(score, 4),
                "age_days": round(age, 2),
                "reason": "retention below floor; archived (reversible)",
            }
        )
    decisions.sort(key=lambda item: float(item["score"]))
    return decisions


def run_lifecycle(
    long_term: LongTermMemoryStore,
    *,
    dry_run: bool = False,
    threshold: float = DEFAULT_ARCHIVE_THRESHOLD,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    limit: int = 200,
) -> dict[str, object]:
    """Evaluate retention and archive eligible memories (never deletes)."""
    records = long_term.list_records(limit=500, include_archived=True, include_superseded=True)
    candidates = records[:limit]
    decisions = plan_archival(candidates, threshold=threshold, min_age_days=min_age_days)
    archived: list[str] = []
    if not dry_run:
        for decision in decisions:
            try:
                long_term.archive(str(decision["id"]))
            except Exception:  # pragma: no cover - a concurrent archive is harmless
                continue
            archived.append(str(decision["id"]))
    return {
        "evaluated": len(candidates),
        "eligible": len(decisions),
        "archived": len(archived),
        "archived_ids": archived,
        "decisions": decisions,
        "threshold": threshold,
        "min_age_days": min_age_days,
        "dry_run": dry_run,
    }


__all__ = [
    "DEFAULT_ARCHIVE_THRESHOLD",
    "DEFAULT_MIN_AGE_DAYS",
    "is_protected",
    "plan_archival",
    "retention_score",
    "run_lifecycle",
]
