"""Task quality benchmark CLI (spec §11, §29.2).

Measures the same three strategies but focuses on quality: deterministic fact recall,
forbidden claims and (when a human label file is provided) task success.  Without labels,
``task_success`` remains ``null`` — the runner never fabricates a quality score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from benchmarks.dataset import DEFAULT_DATASET, RESULTS_DIR, load_dataset
from benchmarks.runner import STRATEGIES, BenchmarkRunner, ContextEchoProvider
from nanomemory.core.config import get_settings
from nanomemory.providers import build_provider


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NanoMemory task quality benchmark")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    parser.add_argument("--labels", default="", help="JSON file: {case_id: 0|1|2}")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    dataset = load_dataset(args.dataset)
    strategies = STRATEGIES if args.strategy == "all" else (args.strategy,)
    labels = None
    if args.labels:
        labels = json.loads(open(args.labels, encoding="utf-8").read())  # noqa: SIM115
    provider = ContextEchoProvider()
    if settings.provider != "mock":
        try:
            provider = build_provider(settings)  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            print(f"provider configuration error: {type(exc).__name__}", file=sys.stderr)
            return 2

    runner = BenchmarkRunner(
        settings, provider=provider, results_dir=args.results_dir, seed=args.seed
    )
    summary = asyncio.run(
        runner.run(dataset, strategies=strategies, runs=max(1, args.runs), labels=labels)
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    print(f"mode: {summary['metadata']['mode']}")
    print(f"quality metric: {summary['metadata']['quality_metric']}")
    print()
    print(f"{'strategy':<14}{'fact recall':>13}{'forbidden':>11}{'task success':>14}")
    print("-" * 52)
    for name, stats in summary["strategies"].items():
        success = stats["task_success_mean"]
        print(
            f"{name:<14}{str(stats['fact_recall_rate']):>13}"
            f"{stats['forbidden_hits']:>11}{str(success):>14}"
        )
    print()
    if not labels:
        print(
            "task_success is null because no human label file was provided "
            "(use --labels labels.json)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
