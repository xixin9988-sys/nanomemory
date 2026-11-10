"""Retrieval-quality benchmark (P0 acceptance metrics).

Measures the hybrid retriever against the same dataset used by the token benchmark and
compares it with the legacy keyword scoring that produced the original 39% recall figure.
Metrics follow the 2.0 spec: Fact Recall@K, Precision@K, Provenance Coverage, and two
behavioural checks (temporal update accuracy, conflict detection).

Run::

    python benchmarks/retrieval_benchmark.py [--k 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.core.locks import LockManager  # noqa: E402
from nanomemory.core.long_term_memory import LongTermMemoryStore  # noqa: E402
from nanomemory.core.memory_models import (  # noqa: E402
    Confidence,
    MemoryStatus,
    MemoryType,
    Provenance,
    SourceKind,
    utc_now_iso,
)
from nanomemory.core.memory_retriever import _keywords, rank_records  # noqa: E402
from nanomemory.core.memory_validator import MemoryValidator  # noqa: E402

DATASET = PROJECT_ROOT / "benchmarks" / "datasets" / "memory_bench_long.json"
RESULTS = PROJECT_ROOT / "benchmarks" / "results"
REPORT = PROJECT_ROOT / "reports" / "phase10_retrieval_benchmark.md"

_NOISE = [
    "The dashboard uses a dark theme by default.",
    "Episodes are stored as JSONL and rotated monthly.",
    "The backup manifest lists SHA-256 hashes for every file.",
    "A rate limit of 60 requests per minute applies to the API.",
    "The compressor triggers at ninety percent of the budget.",
    "Working memory keeps a Markdown document and a JSON sidecar.",
    "Locks are ordered maintenance, backup, working, long-term, episodic, metrics.",
    "Metrics rotate when the JSONL file exceeds one megabyte.",
    "The CLI exposes health and benchmark subcommands.",
    "The provider defaults to a deterministic mock so no API key is needed.",
]


def _store(tmp: Path) -> LongTermMemoryStore:
    settings = build_settings(storage_root=tmp / "storage")
    locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
    store = LongTermMemoryStore(settings, locks, MemoryValidator(settings))
    store.ensure_storage()
    return store


def _add(
    store: LongTermMemoryStore,
    content: str,
    *,
    memory_type: MemoryType = MemoryType.FACT,
    importance: int = 3,
    user_id: str = "bench",
    source_kind: SourceKind = SourceKind.USER,
) -> str:
    record = store.add(
        {
            "content": content,
            "category": "general",
            "memory_type": memory_type.value,
            "importance": importance,
            "confidence": Confidence.HIGH.value,
            "source_type": "user",
            "source": "benchmark",
            "user_id": user_id,
            "valid_from": utc_now_iso(),
            "provenance": Provenance(
                source_kind=source_kind, source_ref="benchmark", created_by="benchmark"
            ).model_dump(mode="json"),
        },
        confirm=True,
    )
    return record.id


def _matches(content: str, terms: list[str]) -> bool:
    lowered = content.lower()
    return any(term.lower() in lowered for term in terms)


def _legacy_score(record: Any, task_keywords: set[str]) -> float:
    """The pre-2.0 keyword scoring, kept only to measure the improvement."""
    record_keywords = _keywords(record.content) | set(record.tags) | _keywords(record.category)
    overlap = task_keywords & record_keywords
    score = 0.0
    if overlap:
        score += 3.0 * min(len(overlap), 5) / 5
    tag_overlap = task_keywords & set(record.tags)
    if tag_overlap:
        score += 5.0
    if record.category.lower() in task_keywords:
        score += 2.0
    if record.importance >= 4:
        score += (record.importance - 3) * 0.5
    if record.confidence.value == "high":
        score += 0.5
    if record.status in {MemoryStatus.NEEDS_REVIEW, MemoryStatus.CONFLICT}:
        score -= 1.0
    return score


def _legacy_top(records: list[Any], task: str, k: int) -> list[Any]:
    tokens = _keywords(task)
    ranked = sorted(records, key=lambda record: _legacy_score(record, tokens), reverse=True)
    return [record for record in ranked[:k] if _legacy_score(record, tokens) > 0]


def evaluate(k: int = 5) -> dict[str, Any]:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    tmp = Path(tempfile.mkdtemp(prefix="nm-retrieval-"))
    store = _store(tmp)
    created = 0
    for case in cases:
        for fact in case["facts"]:
            _add(store, fact, importance=4)
            created += 1
        for forbidden in case.get("forbidden_claims", []):
            _add(store, f"Wrong claim: {forbidden}", importance=2)
            created += 1
    for noise in _NOISE:
        _add(store, noise, importance=2)
        created += 1

    records = store.list_records(limit=500)
    recall_hits = 0
    recall_total = 0
    precision_hits = 0
    precision_total = 0
    provenance_hits = 0
    legacy_hits = 0
    legacy_total = 0
    per_case: list[dict[str, Any]] = []

    for case in cases:
        required = case.get("required_facts", [])
        selected = [record for record, _score, _signals in rank_records(records, case["task"])][:k]
        legacy = _legacy_top(records, case["task"], k)

        case_hits = 0
        for fact in required:
            terms = fact.get("match_terms", [])
            recall_total += 1
            if any(_matches(record.content, terms) for record in selected):
                recall_hits += 1
                case_hits += 1
            legacy_total += 1
            if any(_matches(record.content, terms) for record in legacy):
                legacy_hits += 1

        for record in selected:
            precision_total += 1
            if any(_matches(record.content, fact.get("match_terms", [])) for fact in required):
                precision_hits += 1
        for record in selected:
            if record.provenance.source_kind is SourceKind.USER:
                provenance_hits += 1

        per_case.append(
            {
                "id": case["id"],
                "required": len(required),
                "hit": case_hits,
                "recall_at_k": round(case_hits / len(required), 4) if required else 0.0,
            }
        )

    # Behavioural checks: temporal update and conflict detection.
    update_store = _store(Path(tempfile.mkdtemp(prefix="nm-retrieval-update-")))
    old_id = _add(
        update_store,
        "I use Python for my services.",
        memory_type=MemoryType.PREFERENCE,
        importance=4,
    )
    new_id = _add(
        update_store, "I use Rust for my services.", memory_type=MemoryType.PREFERENCE, importance=4
    )
    old_record = update_store.get(old_id)
    temporal_ok = (
        old_record.status is MemoryStatus.SUPERSEDED
        and old_record.superseded_by == new_id
        and update_store.get(new_id).supersedes == [old_id]
    )
    conflict_store = _store(Path(tempfile.mkdtemp(prefix="nm-retrieval-conflict-")))
    _add(conflict_store, "The API runs on port 8000.", importance=3)
    duplicate = _add(conflict_store, "The API runs on port 8000.", importance=3)
    conflict_ok = conflict_store.get(duplicate).status is MemoryStatus.CONFLICT

    metrics = {
        "k": k,
        "records": created,
        "cases": len(cases),
        "fact_recall_at_k": round(recall_hits / recall_total, 4) if recall_total else 0.0,
        "legacy_recall_at_k": round(legacy_hits / legacy_total, 4) if legacy_total else 0.0,
        "precision_at_k": round(precision_hits / precision_total, 4) if precision_total else 0.0,
        "provenance_coverage": round(provenance_hits / precision_total, 4)
        if precision_total
        else 0.0,
        "temporal_update_accuracy": 1.0 if temporal_ok else 0.0,
        "conflict_detection_accuracy": 1.0 if conflict_ok else 0.0,
        "per_case": per_case,
    }
    return metrics


def _markdown(metrics: dict[str, Any]) -> str:
    return (
        "# Retrieval benchmark (P0)\n\n"
        f"- records in store: {metrics['records']} across {metrics['cases']} cases\n"
        f"- k = {metrics['k']}\n\n"
        "| metric | value |\n| --- | --- |\n"
        f"| Fact Recall@{metrics['k']} (hybrid v2) | {metrics['fact_recall_at_k']:.2%} |\n"
        f"| Fact Recall@{metrics['k']} (legacy keyword) | {metrics['legacy_recall_at_k']:.2%} |\n"
        f"| Precision@{metrics['k']} | {metrics['precision_at_k']:.2%} |\n"
        f"| Provenance coverage | {metrics['provenance_coverage']:.2%} |\n"
        f"| Temporal update accuracy | {metrics['temporal_update_accuracy']:.0%} |\n"
        f"| Conflict detection accuracy | {metrics['conflict_detection_accuracy']:.0%} |\n\n"
        "Per case:\n\n| case | required | hit | recall@k |\n| --- | --- | --- | --- |\n"
        + "\n".join(
            f"| {row['id']} | {row['required']} | {row['hit']} | {row['recall_at_k']:.2f} |"
            for row in metrics["per_case"]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval-quality benchmark")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    metrics = evaluate(k=args.k)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("-", "")[:15]
    (RESULTS / f"retrieval-{stamp}.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(_markdown(metrics), encoding="utf-8")
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(_markdown(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
