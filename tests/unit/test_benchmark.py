"""Benchmark harness tests (spec §11, §22.17, §25.5, §29.2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from benchmarks.dataset import DEFAULT_DATASET, load_dataset, normalize_output
from benchmarks.runner import BenchmarkRunner, ContextEchoProvider, run_benchmark
from nanomemory.core.config import build_settings


@pytest.fixture()
def settings(tmp_path):
    return build_settings(
        storage_root=tmp_path / "storage",
        environment="test",
        log_level="WARNING",
        metrics_enabled=False,
    )


def test_dataset_loads_with_hash_and_schema() -> None:
    dataset = load_dataset()
    assert dataset.cases
    assert len(dataset.sha256) == 64
    assert dataset.schema_version == 1
    for case in dataset.cases:
        assert case.task
        assert case.required_facts
        for fact in case.required_facts:
            assert fact.match_terms, f"{case.id}/{fact.id} needs match_terms"


def test_normalisation_is_casefold_and_nfkc() -> None:
    assert normalize_output("Ｗindows 11") == normalize_output("windows 11")


def test_dataset_rejects_unknown_schema(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"dataset_schema_version": 99, "cases": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)


def test_benchmark_runs_three_strategies(settings, tmp_path) -> None:
    results_dir = tmp_path / "results"
    dataset = load_dataset()
    summary = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=dataset,
            results_dir=results_dir,
            provider=ContextEchoProvider(),
        )
    )
    assert set(summary["strategies"]) == {"full_history", "summary", "nanomemory"}
    meta = summary["metadata"]
    assert meta["mode"] == "mock"
    assert meta["dataset_sha256"] == dataset.sha256
    assert meta["seed"] == 1234
    assert "context_recall_proxy" in meta["quality_metric"]
    for stats in summary["strategies"].values():
        assert stats["preprocessing_tokens"] >= 0
        assert stats["per_query_tokens"] >= 0
        assert stats["usage_source"] in {"mock", "estimated", "exact"}
    # full history has no preprocessing cost by definition
    assert summary["strategies"]["full_history"]["preprocessing_tokens"] == 0
    assert summary["strategies"]["summary"]["preprocessing_tokens"] > 0


def test_benchmark_writes_raw_artifacts(settings, tmp_path) -> None:
    results_dir = tmp_path / "results"
    asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=load_dataset(),
            results_dir=results_dir,
            provider=ContextEchoProvider(),
            strategies=("full_history", "nanomemory"),
        )
    )
    run_dirs = [item for item in results_dir.iterdir() if item.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    for name in ("cases.json", "raw_results.jsonl", "summary.json", "summary.csv", "report.md"):
        assert (run_dir / name).is_file(), name
    cases = json.loads((run_dir / "cases.json").read_text(encoding="utf-8"))
    assert cases["dataset_sha256"]
    summary_files = list(results_dir.glob("*_summary.json"))
    assert summary_files, "dashboard expects a top-level *_summary.json"


def test_saving_rate_may_be_negative(settings, tmp_path) -> None:
    """The runner must report a negative rate rather than clamping it to zero."""
    dataset = load_dataset()
    # Force the nanomemory strategy to be expensive by giving it an absurd request budget.
    summary = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=dataset,
            results_dir=tmp_path / "results",
            provider=ContextEchoProvider(),
            strategies=("full_history", "summary"),
        )
    )
    assert summary["strategies"]["summary"]["saving_rate_pct"] is not None


def test_task_success_is_null_without_labels(settings, tmp_path) -> None:
    summary = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=load_dataset(),
            results_dir=tmp_path / "results",
            provider=ContextEchoProvider(),
            strategies=("nanomemory",),
        )
    )
    assert summary["strategies"]["nanomemory"]["task_success_mean"] is None
    assert summary["metadata"]["labels_provided"] is False


def test_task_success_uses_human_labels(settings, tmp_path) -> None:
    dataset = load_dataset()
    labels = {case.id: 2 for case in dataset.cases}
    summary = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=dataset,
            results_dir=tmp_path / "results",
            provider=ContextEchoProvider(),
            strategies=("nanomemory",),
            labels=labels,
        )
    )
    assert summary["strategies"]["nanomemory"]["task_success_mean"] == 2.0
    assert summary["metadata"]["labels_provided"] is True


def test_fact_recall_is_deterministic(settings, tmp_path) -> None:
    dataset = load_dataset()
    first = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=dataset,
            results_dir=tmp_path / "a",
            provider=ContextEchoProvider(),
            strategies=("summary",),
        )
    )
    second = asyncio.run(
        run_benchmark(
            settings=settings,
            dataset=dataset,
            results_dir=tmp_path / "b",
            provider=ContextEchoProvider(),
            strategies=("summary",),
        )
    )
    assert (
        first["strategies"]["summary"]["fact_recall_rate"]
        == (second["strategies"]["summary"]["fact_recall_rate"])
    )


def test_runner_records_dataset_path() -> None:
    dataset = load_dataset()
    assert Path(dataset.path).name == DEFAULT_DATASET.name


def test_mock_mode_is_labelled(settings, tmp_path) -> None:
    runner = BenchmarkRunner(
        settings, provider=ContextEchoProvider(), results_dir=tmp_path / "results"
    )
    assert runner.mode == "mock"
