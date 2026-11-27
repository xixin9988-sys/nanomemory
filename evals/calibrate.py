"""Pick the refusal floor from measurements, not taste.

The runner seeds the eval corpus once, asks every question with allow_weak=True and
min_score=0, then sweeps candidate NANOMEMORY_VECTOR_CONTENT_FLOOR values offline.  For each
floor it reports the resulting Recall@K and refusal accuracy, so the trade-off curve is
reviewable and the chosen constant can be re-derived when the embedding provider changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.run_eval import DEFAULT_DATASET, seed_corpus  # noqa: E402

from nanomemory.core.config import build_settings  # noqa: E402
from nanomemory.vector.schemas import TenantContext  # noqa: E402
from nanomemory.vector.service import VectorMemoryService  # noqa: E402


async def collect(
    dataset: dict, *, storage: Path, provider: str, dimension: int, k: int
) -> list[dict]:
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
    context = TenantContext(tenant_id="eval-tenant", user_id="eval-user")
    rows: list[dict] = []
    try:
        id_by_key = await seed_corpus(service, context, dataset)
        for case in dataset["cases"]:
            expected = [id_by_key[key] for key in case.get("expected_memory_ids", [])]
            forbidden = [id_by_key[key] for key in case.get("forbidden_memory_ids", [])]
            result = await service.search(
                context,
                case["query"],
                limit=k,
                store_context=False,
                allow_weak=True,
                min_score=0.0,
            )
            ids = [hit.id for hit in result.items]
            rows.append(
                {
                    "id": case["id"],
                    "type": case["type"],
                    "signal": result.content_signal,
                    "found": any(memory_id in ids for memory_id in expected),
                    "forbidden": any(memory_id in ids for memory_id in forbidden),
                }
            )
    finally:
        service.close()
    return rows


def sweep(rows: list[dict], floors: list[float], k: int) -> list[dict]:
    recall_rows = [row for row in rows if row["type"] == "recall"]
    refusal_rows = [row for row in rows if row["type"] == "refusal"]
    results = []
    for floor in floors:
        recall = sum(1 for row in recall_rows if row["found"] and row["signal"] >= floor) / len(
            recall_rows
        )
        refusal = sum(1 for row in refusal_rows if row["signal"] < floor) / len(refusal_rows)
        stale = sum(1 for row in rows if row["type"] == "stale" and row["forbidden"])
        trap = sum(1 for row in rows if row["type"] == "trap" and row["forbidden"])
        results.append(
            {
                "floor": round(floor, 3),
                "recall": round(recall, 4),
                "refusal": round(refusal, 4),
                "balanced": round((recall + refusal) / 2, 4),
                "stale": stale,
                "trap": trap,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep the refusal floor for the eval set.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--provider", default="hashing")
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    storage = Path(tempfile.mkdtemp(prefix="nm-cal-"))
    try:
        rows = asyncio.run(
            collect(
                dataset,
                storage=storage,
                provider=args.provider,
                dimension=args.dimension,
                k=args.k,
            )
        )
    finally:
        shutil.rmtree(storage, ignore_errors=True)
    floors = [round(0.06 + 0.02 * index, 3) for index in range(18)]
    results = sweep(rows, floors, args.k)
    print("| floor | recall@k | refusal | balanced | stale | trap |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in results:
        print(
            f"| {row['floor']} | {row['recall']:.3f} | {row['refusal']:.3f} | "
            f"{row['balanced']:.3f} | {row['stale']} | {row['trap']} |"
        )
    eligible = [row for row in results if row["recall"] >= 0.85]
    best = max(eligible or results, key=lambda row: (row["refusal"], row["recall"]))
    print(
        f"\nrecommended floor: {best['floor']} "
        f"(recall {best['recall']:.3f}, refusal {best['refusal']:.3f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
