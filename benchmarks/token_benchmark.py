"""Token efficiency benchmark CLI (spec §11, §16).

Examples::

    python -m benchmarks.token_benchmark --strategy all
    python -m benchmarks.token_benchmark --strategy nanomemory --runs 3
    python -m benchmarks.token_benchmark --dataset benchmarks/datasets/memory_bench.json

The default provider is a deterministic context-echo mock.  Real measurements require
``NANOMEMORY_PROVIDER=openai_compatible`` plus a key; results are then labelled ``real``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from benchmarks.dataset import DEFAULT_DATASET, RESULTS_DIR, load_dataset
from benchmarks.runner import STRATEGIES, BenchmarkRunner, ContextEchoProvider
from nanomemory.core.config import get_settings
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.providers import build_provider


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NanoMemory token efficiency benchmark")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument(
        "--strategy",
        default="all",
        choices=["all", *STRATEGIES],
        help="Which context strategy to measure (default: all)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Repeats per case")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    parser.add_argument(
        "--labels",
        default="",
        help="Optional JSON file mapping case id to human task_success label (0/1/2)",
    )
    parser.add_argument("--json", action="store_true", help="Print the summary JSON only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    dataset = load_dataset(args.dataset)
    strategies = STRATEGIES if args.strategy == "all" else (args.strategy,)
    labels: dict[str, int] | None = None
    if args.labels:
        labels = json.loads(open(args.labels, encoding="utf-8").read())  # noqa: SIM115
        for case_id, value in labels.items():
            if value not in {0, 1, 2}:
                print(f"invalid label for {case_id}: {value} (expected 0, 1 or 2)", file=sys.stderr)
                return 2

    if settings.provider == "mock":
        provider = ContextEchoProvider()
    else:
        try:
            provider = build_provider(settings)
        except Exception as exc:  # noqa: BLE001 - configuration error is reported below
            print(f"provider configuration error: {type(exc).__name__}", file=sys.stderr)
            return 2
    # Validate that the secret scanner never sees a real key in dataset content.
    validator = MemoryValidator(settings)
    for case in dataset.cases:
        validator.sanitise_value(case.to_dict(), location=f"dataset.{case.id}")

    runner = BenchmarkRunner(
        settings, provider=provider, results_dir=args.results_dir, seed=args.seed
    )
    summary = asyncio.run(
        runner.run(dataset, strategies=strategies, runs=max(1, args.runs), labels=labels)
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"mode: {summary['metadata']['mode']}")
        print(f"run:  {summary['metadata']['run_id']}")
        print(f"dataset sha256: {summary['metadata']['dataset_sha256']}")
        print()
        header = f"{'strategy':<14}{'tokens':>10}{'pre':>10}{'per query':>12}{'recall':>9}{'saving %':>10}"
        print(header)
        print("-" * len(header))
        for name, stats in summary["strategies"].items():
            saving = stats["saving_rate_pct"]
            print(
                f"{name:<14}{stats['total_tokens']:>10}{stats['preprocessing_tokens']:>10}"
                f"{stats['per_query_tokens']:>12}{str(stats['fact_recall_rate']):>9}"
                f"{(f'{saving:.2f}' if saving is not None else 'n/a'):>10}"
            )
        print()
        print(f"artifacts: {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
