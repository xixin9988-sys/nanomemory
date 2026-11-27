"""NanoMemory vector eval runner.

Measures what the product promises, not what the demo shows:

* Recall@K and MRR on answerable questions,
* refusal accuracy on questions whose answer is not in the corpus,
* stale-memory violations (an archived memory served as current),
* an honest count of answer-grounding cases that retrieval cannot judge and that are
  delegated to the gateway's grounded-answer guard,
* latency distribution and a per-case table for inspection.

The dataset is independent of the implementation: it seeds a corpus through the public service
API and queries it through the same API, so the runner works for the offline hashing provider
and for a real embedding endpoint alike.

Usage:
    python evals/run_eval.py --k 5
    python evals/run_eval.py --provider openai_compatible --dimension 1536 --report evals/reports/latest.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.vector.schemas import MemoryMetadata, TenantContext  # noqa: E402
from nanomemory.vector.service import NewMemory, VectorMemoryService  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "evals" / "dataset.json"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


async def seed_corpus(
    service: VectorMemoryService, context: TenantContext, dataset: dict[str, Any]
) -> dict[str, str]:
    """Create the corpus through the public service path; returns key -> memory id."""
    id_by_key: dict[str, str] = {}
    for item in dataset["corpus"]:
        metadata = MemoryMetadata(
            category=item.get("category", "general"),
            tags=item.get("tags", []),
            memory_type=item.get("memory_type", "fact"),
            source="eval-seed",
            attributes={"eval_key": item["key"]},
        )
        outcome = await service.remember(
            context, NewMemory(content=item["content"], metadata=metadata)
        )
        id_by_key[item["key"]] = outcome.record.id
        if item.get("state") == "archived":
            service.archive(context, outcome.record.id)
    return id_by_key


async def evaluate(
    dataset: dict[str, Any],
    *,
    storage: Path,
    provider: str = "hashing",
    dimension: int = 384,
    k: int = 5,
    keep: bool = False,
) -> dict[str, Any]:
    settings = build_settings(
        storage_root=storage,
        environment="test",
        embedding_provider=provider,
        embedding_dimension=dimension,
        log_level="ERROR",
        rate_limit_enabled=False,
    )
    service = VectorMemoryService(settings)
    service.ensure_ready()
    context = TenantContext(
        tenant_id=str(dataset.get("tenant_id") or "eval-tenant"),
        user_id=str(dataset.get("user_id") or "eval-user"),
    )
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    seeded_at = time.perf_counter()
    try:
        id_by_key = await seed_corpus(service, context, dataset)
        seed_ms = round((time.perf_counter() - seeded_at) * 1000, 2)

        for case in dataset["cases"]:
            expected = [id_by_key[key] for key in case.get("expected_memory_ids", [])]
            forbidden = [id_by_key[key] for key in case.get("forbidden_memory_ids", [])]
            started = time.perf_counter()
            result = await service.search(context, case["query"], limit=k, store_context=False)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            ids = [hit.id for hit in result.items]
            rank = next(
                (
                    position
                    for position, memory_id in enumerate(ids, start=1)
                    if memory_id in expected
                ),
                None,
            )
            refused = result.insufficient_context or not ids
            forbidden_hit = any(memory_id in ids for memory_id in forbidden)
            row = {
                "id": case["id"],
                "query": case["query"],
                "type": case["type"],
                "requires": case.get("requires", "lexical"),
                "expected": case.get("expected_memory_ids", []),
                "retrieved": [hit.metadata.attributes.get("eval_key") for hit in result.items],
                "scores": [round(hit.score, 4) for hit in result.items],
                "signal": round(result.content_signal, 4),
                "rank": rank,
                "refused": refused,
                "forbidden_hit": forbidden_hit,
                "latency_ms": round(latency, 2),
                "trace_id": result.trace_id,
            }
            if case["type"] == "recall":
                row["passed"] = rank is not None and not forbidden_hit
            elif case["type"] == "refusal":
                row["passed"] = refused
            elif case["type"] == "grounding":
                # Retrieval cannot judge this case: it is scored by the answer-level guard
                # (see mem-guard's grounded-answer check), so it is reported, not gated.
                row["passed"] = None
            else:  # stale
                row["passed"] = not forbidden_hit
            rows.append(row)
    finally:
        service.close()

    def _of(kind: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["type"] == kind]

    recall_rows = _of("recall")
    refusal_rows = _of("refusal")
    stale_rows = _of("stale")
    grounding_rows = _of("grounding")
    hits = [row for row in recall_rows if row["rank"] is not None]
    recall_at_k = len(hits) / len(recall_rows) if recall_rows else 0.0
    mrr = (
        sum(1.0 / row["rank"] for row in hits if row["rank"]) / len(recall_rows)
        if recall_rows
        else 0.0
    )
    refusal_accuracy = (
        sum(1 for row in refusal_rows if row["refused"]) / len(refusal_rows)
        if refusal_rows
        else 0.0
    )
    stale_violations = sum(1 for row in stale_rows if row["forbidden_hit"])
    scored_rows = [row for row in rows if row["passed"] is not None]
    adversarial = len(refusal_rows) + len(stale_rows) + len(grounding_rows)

    def _subset(name: str) -> dict[str, float]:
        subset = [row for row in recall_rows if row["requires"] == name]
        if not subset:
            return {"cases": 0, "recall": 0.0}
        return {
            "cases": len(subset),
            "recall": round(sum(1 for row in subset if row["rank"]) / len(subset), 4),
        }

    summary = {
        "dataset": dataset.get("name"),
        "provider": provider,
        "dimension": dimension,
        "k": k,
        "cases": len(rows),
        "recall_cases": len(recall_rows),
        "refusal_cases": len(refusal_rows),
        "grounding_cases": len(grounding_rows),
        "adversarial_cases": adversarial,
        "adversarial_share": round(adversarial / len(rows), 4) if rows else 0.0,
        "recall_at_k": round(recall_at_k, 4),
        "mrr": round(mrr, 4),
        "refusal_accuracy": round(refusal_accuracy, 4),
        "false_answer_rate": round(1 - refusal_accuracy, 4),
        "stale_violations": stale_violations,
        "passed_cases": sum(1 for row in scored_rows if row["passed"]),
        "pass_rate": round(sum(1 for row in scored_rows if row["passed"]) / len(scored_rows), 4),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
            "p95": round(_percentile(latencies, 0.95), 2),
        },
        "seed_ms": seed_ms,
        "by_requirement": {"lexical": _subset("lexical"), "semantic": _subset("semantic")},
        "rows": rows,
    }
    if keep:
        summary["storage"] = str(storage)
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# NanoMemory vector eval — {summary['dataset']}",
        "",
        f"- provider: {summary['provider']} (dim {summary['dimension']})",
        f"- cases: {summary['cases']} — {summary['recall_cases']} recall, "
        f"{summary['refusal_cases']} refusal, {summary['adversarial_cases']} adversarial "
        f"({summary['adversarial_share']:.1%}); K={summary['k']}",
        f"- answer-grounding cases (scored by mem-guard, not here): {summary['grounding_cases']}",
        f"- **Recall@{summary['k']}: {summary['recall_at_k']:.1%}**, MRR {summary['mrr']:.3f}",
        f"- **Refusal accuracy: {summary['refusal_accuracy']:.1%}**, "
        f"false-answer rate {summary['false_answer_rate']:.1%}",
        f"- stale-memory violations: {summary['stale_violations']}",
        f"- pass rate: {summary['pass_rate']:.1%} "
        f"({summary['passed_cases']}/{summary['cases'] - summary['grounding_cases']})",
        f"- latency mean {summary['latency_ms']['mean']} ms, p95 {summary['latency_ms']['p95']} ms",
        f"- lexical cases: {summary['by_requirement']['lexical']['cases']} "
        f"(recall {summary['by_requirement']['lexical']['recall']:.1%}), "
        f"semantic cases: {summary['by_requirement']['semantic']['cases']} "
        f"(recall {summary['by_requirement']['semantic']['recall']:.1%})",
        "",
        "| case | type | query | expected | retrieved | rank | signal | refused | result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        expected = ", ".join(row["expected"]) or "—"
        retrieved = ", ".join(str(item) for item in row["retrieved"]) or "—"
        lines.append(
            f"| {row['id']} | {row['type']} | {row['query']} | {expected} | {retrieved} | "
            f"{row['rank'] or '—'} | {row['signal']} | {'yes' if row['refused'] else 'no'} | "
            f"{'N/A' if row['passed'] is None else ('PASS' if row['passed'] else 'FAIL')} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the NanoMemory vector eval set.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--provider", default="hashing")
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--storage", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--min-recall", type=float, default=0.85)
    parser.add_argument("--min-refusal", type=float, default=0.75)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    storage = args.storage or Path(tempfile.mkdtemp(prefix="nm-eval-"))
    try:
        summary = asyncio.run(
            evaluate(
                dataset,
                storage=storage,
                provider=args.provider,
                dimension=args.dimension,
                k=args.k,
                keep=args.keep,
            )
        )
    finally:
        if not args.keep and args.storage is None:
            shutil.rmtree(storage, ignore_errors=True)

    report = render_markdown(summary)
    print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {key: value for key, value in summary.items() if key != "rows"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")

    failures = []
    if summary["recall_at_k"] < args.min_recall:
        failures.append(f"recall {summary['recall_at_k']:.3f} < {args.min_recall}")
    if summary["refusal_accuracy"] < args.min_refusal:
        failures.append(f"refusal {summary['refusal_accuracy']:.3f} < {args.min_refusal}")
    if summary["stale_violations"]:
        failures.append(f"{summary['stale_violations']} stale-memory violations")
    if failures:
        print("\nQUALITY GATE FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("\nQuality gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
