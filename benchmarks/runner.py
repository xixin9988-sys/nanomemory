"""Benchmark runner: Full History vs Summary Memory vs NanoMemory (spec §11, §22.17, §29.2).

Fairness rules implemented here:

* identical cases, identical system prompt and identical context scaffold for all strategies
  (the only variable is the memory content),
* fixed ``seed`` and dataset SHA-256 recorded in every artifact,
* pre-processing cost is counted separately from per-query cost (summary generation and
  memory population are not free),
* deterministic fact recall (NFKC + casefold substring matching on declared ``match_terms``);
  **no LLM judge** is used for recall,
* ``task_success`` is left ``null`` unless a human label file is supplied — the runner never
  invents a quality score,
* mock-mode results are labelled as a *context coverage proxy*, not a model-capability or
  real-API measurement,
* token saving rate may be negative and handles a zero denominator.
"""

from __future__ import annotations

import csv
import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.dataset import (
    RESULT_SCHEMA_VERSION,
    RESULTS_DIR,
    BenchmarkCase,
    Dataset,
    load_dataset,
    normalize_output,
)
from nanomemory.core.config import Settings
from nanomemory.core.context_builder import ContextBuilder
from nanomemory.core.episodic_memory import EpisodicMemoryStore
from nanomemory.core.locks import LockManager
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import MemoryUpdateProposal, RetrievedMemory, TrustLevel
from nanomemory.core.memory_retriever import MemoryRetriever
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.paths import ensure_dir
from nanomemory.core.secret_scanner import SecretScanner
from nanomemory.core.token_counter import TokenCounter
from nanomemory.core.working_memory import WorkingMemoryStore
from nanomemory.providers.base import BaseProvider, LLMResponse

STRATEGIES: tuple[str, ...] = ("full_history", "summary", "nanomemory")


class ContextEchoProvider(BaseProvider):
    """Deterministic provider that echoes the context it received.

    Used only in mock mode.  It measures **whether the context contained the required
    facts** (a context coverage proxy), not whether a real model would answer correctly.
    """

    provider_name = "mock-context-echo"
    model_name = "context-echo"
    is_mock = True

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        del system, max_tokens, temperature
        return LLMResponse(text=prompt, provider=self.provider_name, model=self.model_name)


@dataclass(slots=True)
class CaseResult:
    """One (case, strategy, run) measurement, saved verbatim."""

    result_schema_version: int
    case_id: str
    strategy: str
    run: int
    usage_source: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_tokens: int | None
    latency_ms: float
    fact_recall: list[dict[str, Any]] = field(default_factory=list)
    fact_recall_rate: float = 0.0
    forbidden_hits: list[str] = field(default_factory=list)
    task_success: int | None = None
    response_chars: int = 0
    context_budget: int = 0
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git_commit() -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _extract_answer(text: str) -> str:
    """Model output may be a JSON envelope (production prompt) or plain text."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict) and isinstance(payload.get("response"), str):
                return payload["response"]
        except json.JSONDecodeError:
            pass
    return text


@dataclass(slots=True)
class StrategyAggregate:
    """Aggregated numbers for one strategy."""

    cases: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_tokens: int = 0
    usage_source: str = "estimated"
    latency_ms: float = 0.0
    facts_total: int = 0
    facts_matched: int = 0
    forbidden_hits: int = 0
    task_success_labels: list[int] = field(default_factory=list)
    preprocessing_tokens: int = 0
    cost: float | None = None
    cost_currency: str | None = None
    context_violations: int = 0

    def as_dict(self) -> dict[str, Any]:
        per_query = self.total_tokens / self.cases if self.cases else 0.0
        total_with_preprocessing = self.total_tokens + self.preprocessing_tokens
        return {
            "cases": self.cases,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_tokens": self.estimated_tokens,
            "usage_source": self.usage_source,
            "latency_ms": round(self.latency_ms / self.cases, 2) if self.cases else 0.0,
            "fact_recall_rate": round(self.facts_matched / self.facts_total, 4)
            if self.facts_total
            else None,
            "facts_matched": self.facts_matched,
            "facts_total": self.facts_total,
            "forbidden_hits": self.forbidden_hits,
            "task_success_mean": round(
                sum(self.task_success_labels) / len(self.task_success_labels), 3
            )
            if self.task_success_labels
            else None,
            "task_success_labeled_cases": len(self.task_success_labels),
            "per_query_tokens": round(per_query, 2),
            "preprocessing_tokens": self.preprocessing_tokens,
            "total_tokens_with_preprocessing": total_with_preprocessing,
            "cost": round(self.cost, 8) if self.cost is not None else None,
            "cost_currency": self.cost_currency,
            "cost_note": None if self.cost is not None else "N/A (no price table configured)",
            "context_budget_violations": self.context_violations,
        }


class BenchmarkRunner:
    """Runs the three context strategies and writes reproducible artifacts."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: BaseProvider | None = None,
        results_dir: Path | None = None,
        seed: int = 1234,
    ) -> None:
        self.settings = settings
        self.provider = provider or ContextEchoProvider()
        self.mode = "mock" if getattr(self.provider, "is_mock", False) else "real"
        self.results_dir = Path(results_dir) if results_dir else RESULTS_DIR
        self.seed = seed
        self.counter = TokenCounter(settings)
        self.context_builder = ContextBuilder(settings, counter=self.counter)
        self.commit = _git_commit()

    # ------------------------------------------------------------------ setup
    def _build_memory_stack(
        self, workdir: Path
    ) -> tuple[WorkingMemoryStore, LongTermMemoryStore, EpisodicMemoryStore, MemoryRetriever]:
        """Create a throw-away memory stack used by the NanoMemory strategy."""
        from nanomemory.core.config import build_settings

        storage = workdir / "storage"
        settings = build_settings(
            storage_root=storage,
            environment="test",
            log_level="WARNING",
            metrics_enabled=False,
            request_token_budget=self.settings.request_token_budget,
            context_token_budget=self.settings.context_token_budget,
            working_snapshot_retention=1,
        )
        locks = LockManager(settings.locks_dir, storage_root=settings.storage_root)
        validator = MemoryValidator(settings, SecretScanner())
        working = WorkingMemoryStore(settings, locks, validator)
        long_term = LongTermMemoryStore(settings, locks, validator)
        episodic = EpisodicMemoryStore(settings, locks, validator)
        working.registry.ensure_default()
        long_term.ensure_storage()
        episodic.ensure_storage()
        retriever = MemoryRetriever(settings, working, long_term, episodic, counter=self.counter)
        return working, long_term, episodic, retriever

    # ------------------------------------------------------------------ prompts
    def _prompt_for(
        self, case: BenchmarkCase, strategy: str, retriever: MemoryRetriever | None, session_id: str
    ) -> tuple[str, str, int, int]:
        """Return ``(system, user, estimated_tokens, budget)`` for one strategy."""
        if strategy == "full_history":
            content = "Conversation history:\n" + "\n".join(case.history)
            item = RetrievedMemory(
                kind="episodic",
                id=f"{case.id}-history",
                content=content,
                tokens=self.counter.estimate(content),
                source="benchmark.history",
                trust_level=TrustLevel.UNTRUSTED,
                reason="full history strategy",
            )
        elif strategy == "summary":
            content = "Summary of the conversation:\n" + case.summary
            item = RetrievedMemory(
                kind="long_term",
                id=f"{case.id}-summary",
                content=content,
                tokens=self.counter.estimate(content),
                source="benchmark.summary",
                trust_level=TrustLevel.UNTRUSTED,
                reason="summary strategy",
            )
        else:
            from nanomemory.core.memory_models import RetrievalResult

            assert retriever is not None
            retrieval = retriever.retrieve(case.task, session_id=session_id)
            # The benchmark measures memory context only; tool schemas are identical for
            # every strategy and are therefore excluded from all three prompts.
            bundle = self.context_builder.build(
                task=case.task, retrieval=retrieval, tool_schema_tokens=0
            )
            return (
                bundle.system_message,
                bundle.user_message,
                bundle.estimated_request_tokens,
                bundle.request_budget,
            )
        from nanomemory.core.memory_models import RetrievalResult

        retrieval = RetrievalResult(
            current_task=case.task,
            session_id=session_id,
            selected=[item],
            estimated_tokens=item.tokens,
            token_budget=self.settings.memory_token_limit,
        )
        # Give the baseline strategies the same request budget as production so an oversized
        # history is measured rather than truncated silently.
        budget = max(self.settings.request_token_budget, item.tokens + 2000)
        bundle = self.context_builder.build(
            task=case.task, retrieval=retrieval, request_budget=budget
        )
        return (
            bundle.system_message,
            bundle.user_message,
            bundle.estimated_request_tokens,
            budget,
        )

    # ------------------------------------------------------------------ run
    async def run(
        self,
        dataset: Dataset,
        *,
        strategies: tuple[str, ...] = STRATEGIES,
        runs: int = 1,
        labels: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        started_at = _now()
        run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{self.mode}-{run_stamp}-{self.commit}"
        run_dir = ensure_dir(
            self.results_dir / run_id, root=self.results_dir.parent, label="benchmark results"
        )
        # The throw-away memory stack lives in a *short* path: transaction directories add
        # ``.txn/<32 hex>/`` and Windows' 260-character limit is easy to exceed under
        # ``benchmarks/results/<run_id>/``.
        workdir = ensure_dir(
            Path(__file__).resolve().parents[1] / ".bw" / uuid.uuid4().hex[:8],
            root=Path(__file__).resolve().parents[1],
            label="benchmark work",
        )
        working, long_term, episodic, retriever = self._build_memory_stack(workdir)
        session_id = working.settings.default_session_id

        # ---- pre-processing: populate memory with the dataset facts
        preprocessing_nanomemory = 0
        for case in dataset.cases:
            working.ensure_session(session_id)
            working.update(
                session_id,
                MemoryUpdateProposal(
                    current_goal=case.task,
                    in_progress=[],
                    allow_task_removal=True,
                ),
                expected_version=working.read(session_id).meta.memory_version,
            )
            for index, fact in enumerate(case.facts):
                long_term.add(
                    {
                        "id": f"{case.id}-fact-{index:02d}",
                        "content": fact,
                        "category": "benchmark",
                        "tags": [case.id],
                        "source": f"benchmark:{case.id}",
                        "source_type": "import",
                        "importance": 4,
                    }
                )
                preprocessing_nanomemory += self.counter.estimate(fact)
        preprocessing_summary = sum(
            self.counter.estimate("\n".join(case.history)) + self.counter.estimate(case.summary)
            for case in dataset.cases
        )

        # ---- measure
        results: list[CaseResult] = []
        for case in dataset.cases:
            for run_index in range(1, runs + 1):
                for strategy in strategies:
                    system, user, estimated, budget = self._prompt_for(
                        case, strategy, retriever, session_id
                    )
                    started = time.perf_counter()
                    response = await self.provider.generate(prompt=user, system=system)
                    latency_ms = round((time.perf_counter() - started) * 1000, 3)
                    answer = _extract_answer(response.text)
                    normalized = normalize_output(answer)
                    recall: list[dict[str, Any]] = []
                    matched = 0
                    for fact in case.required_facts:
                        hit, terms = fact.matches(normalized)
                        matched += 1 if hit else 0
                        recall.append(
                            {
                                "id": fact.id,
                                "text": fact.text,
                                "matched": hit,
                                "matched_terms": terms,
                            }
                        )
                    forbidden = [
                        claim
                        for claim in case.forbidden_claims
                        if normalize_output(claim) in normalized
                    ]
                    usage = self.counter.build_usage(
                        provider=self.provider.provider_name,
                        model=self.provider.model_name,
                        prompt_text=user,
                        completion_text=response.text,
                        latency_ms=latency_ms,
                        provider_usage=response.usage,
                        kind="benchmark",
                        benchmark=run_id,
                        mock=self.mode == "mock",
                    )
                    results.append(
                        CaseResult(
                            result_schema_version=RESULT_SCHEMA_VERSION,
                            case_id=case.id,
                            strategy=strategy,
                            run=run_index,
                            usage_source=usage.usage_source,
                            prompt_tokens=usage.prompt_tokens,
                            completion_tokens=usage.completion_tokens,
                            total_tokens=usage.total_tokens,
                            estimated_tokens=usage.estimated_tokens,
                            latency_ms=latency_ms,
                            fact_recall=recall,
                            fact_recall_rate=(
                                matched / len(case.required_facts) if case.required_facts else 0.0
                            ),
                            forbidden_hits=forbidden,
                            task_success=(labels or {}).get(case.id),
                            response_chars=len(answer),
                            context_budget=budget,
                            notes=f"estimated_request_tokens={estimated}",
                        )
                    )

        summary = self._aggregate(
            dataset=dataset,
            results=results,
            strategies=strategies,
            runs=runs,
            run_id=run_id,
            started_at=started_at,
            preprocessing={
                "summary": preprocessing_summary,
                "nanomemory": preprocessing_nanomemory,
                "full_history": 0,
            },
            labels=labels,
        )
        self._write_artifacts(run_dir, dataset, results, summary)
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
        return summary

    # ------------------------------------------------------------------ aggregation
    def _aggregate(
        self,
        *,
        dataset: Dataset,
        results: list[CaseResult],
        strategies: tuple[str, ...],
        runs: int,
        run_id: str,
        started_at: str,
        preprocessing: dict[str, int],
        labels: dict[str, int] | None,
    ) -> dict[str, Any]:
        aggregates: dict[str, StrategyAggregate] = {}
        for strategy in strategies:
            subset = [result for result in results if result.strategy == strategy]
            aggregate = StrategyAggregate(
                usage_source="mock" if self.mode == "mock" else "estimated"
            )
            for result in subset:
                aggregate.cases += 1
                tokens = result.total_tokens or result.estimated_tokens or 0
                if result.usage_source == "exact":
                    aggregate.prompt_tokens += result.prompt_tokens or 0
                    aggregate.completion_tokens += result.completion_tokens or 0
                    aggregate.total_tokens += tokens
                    aggregate.usage_source = "exact"
                else:
                    aggregate.estimated_tokens += tokens
                    aggregate.total_tokens += tokens
                    if aggregate.usage_source != "exact":
                        aggregate.usage_source = result.usage_source
                aggregate.latency_ms += result.latency_ms
                aggregate.facts_total += len(result.fact_recall)
                aggregate.facts_matched += sum(1 for item in result.fact_recall if item["matched"])
                aggregate.forbidden_hits += len(result.forbidden_hits)
                if result.task_success is not None:
                    aggregate.task_success_labels.append(result.task_success)
                if result.estimated_tokens is not None:
                    aggregate.estimated_tokens += 0  # already counted above
                if tokens > result.context_budget:
                    aggregate.context_violations += 1
            aggregate.preprocessing_tokens = preprocessing.get(strategy, 0)
            aggregates[strategy] = aggregate

        full = aggregates.get("full_history")
        full_tokens = full.total_tokens if full else 0
        strategy_dicts: dict[str, Any] = {}
        for strategy, aggregate in aggregates.items():
            payload = aggregate.as_dict()
            if full_tokens:
                payload["saving_rate_pct"] = round(
                    (full_tokens - aggregate.total_tokens) / full_tokens * 100, 2
                )
            else:
                payload["saving_rate_pct"] = None
                payload["saving_note"] = "undefined: full-history token count was zero"
            strategy_dicts[strategy] = payload

        metadata = {
            "run_id": run_id,
            "mode": self.mode,
            "provider": self.provider.provider_name,
            "model": self.provider.model_name,
            "seed": self.seed,
            "commit": self.commit,
            "generated_at": started_at,
            "finished_at": _now(),
            "dataset_name": dataset.name,
            "dataset_path": dataset.path,
            "dataset_sha256": dataset.sha256,
            "dataset_schema_version": dataset.schema_version,
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "token_estimator": self.counter.estimator.name,
            "prices_configured": self.counter.prices.configured,
            "context_token_budget": self.settings.context_token_budget,
            "memory_token_limit": self.settings.memory_token_limit,
            "request_token_budget": self.settings.request_token_budget,
            "runs_per_case": runs,
            "case_count": len(dataset.cases),
            "labels_provided": bool(labels),
            "quality_metric": (
                "context_recall_proxy (mock mode measures whether the context contained the "
                "facts; it is not a model-capability or real-API measurement)"
                if self.mode == "mock"
                else "deterministic fact recall over the model answer"
            ),
            "notes": [
                "All strategies use the same system prompt and context scaffold.",
                "Pre-processing tokens (summary generation, memory population) are reported "
                "separately and included in total_tokens_with_preprocessing.",
                "task_success stays null unless a human label file is supplied.",
                "Token counts are provider-exact only when the provider reported usage.",
            ],
            "excluded_cases": [],
        }
        return {
            "schema_version": 1,
            "metadata": metadata,
            "case_count": len(dataset.cases),
            "strategies": strategy_dicts,
            "labels": labels or {},
        }

    # ------------------------------------------------------------------ artifacts
    def _write_artifacts(
        self,
        run_dir: Path,
        dataset: Dataset,
        results: list[CaseResult],
        summary: dict[str, Any],
    ) -> None:
        (run_dir / "cases.json").write_text(
            json.dumps(
                {
                    "dataset_schema_version": dataset.schema_version,
                    "dataset_sha256": dataset.sha256,
                    "cases": dataset.case_dicts(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        with open(run_dir / "raw_results.jsonl", "w", encoding="utf-8", newline="\n") as handle:
            for result in results:
                handle.write(json.dumps(result.as_dict(), ensure_ascii=False) + "\n")
        summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        (run_dir / "summary.json").write_text(summary_text, encoding="utf-8")
        # A top-level copy is what the dashboard globs for (*_summary.json).
        stamp = summary["metadata"]["run_id"]
        (self.results_dir / f"{stamp}_summary.json").write_text(summary_text, encoding="utf-8")

        with open(run_dir / "summary.csv", "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "strategy",
                    "cases",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "preprocessing_tokens",
                    "total_with_preprocessing",
                    "fact_recall_rate",
                    "task_success_mean",
                    "latency_ms",
                    "saving_rate_pct",
                ]
            )
            for strategy, stats in summary["strategies"].items():
                writer.writerow(
                    [
                        strategy,
                        stats["cases"],
                        stats["prompt_tokens"],
                        stats["completion_tokens"],
                        stats["total_tokens"],
                        stats["preprocessing_tokens"],
                        stats["total_tokens_with_preprocessing"],
                        stats["fact_recall_rate"],
                        stats["task_success_mean"],
                        stats["latency_ms"],
                        stats["saving_rate_pct"],
                    ]
                )
        (run_dir / "report.md").write_text(self._render_report(summary), encoding="utf-8")

    def _render_report(self, summary: dict[str, Any]) -> str:
        meta = summary["metadata"]
        lines = [
            "# NanoMemory Benchmark Report",
            "",
            f"- run: `{meta['run_id']}`",
            f"- mode: **{meta['mode']}** ({meta['provider']} / {meta['model']})",
            f"- dataset: `{meta['dataset_name']}` sha256 `{meta['dataset_sha256']}`",
            f"- cases: {meta['case_count']} · runs per case: {meta['runs_per_case']} · seed {meta['seed']}",
            f"- commit: `{meta['commit']}` · estimator: `{meta['token_estimator']}`",
            f"- generated: {meta['generated_at']}",
            "",
            f"> {meta['quality_metric']}",
            "",
            "| strategy | total tokens | preprocessing | per query | fact recall | task success | saving % |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for strategy, stats in summary["strategies"].items():
            lines.append(
                "| {strategy} | {total} | {pre} | {per} | {recall} | {success} | {saving} |".format(
                    strategy=strategy,
                    total=stats["total_tokens"],
                    pre=stats["preprocessing_tokens"],
                    per=stats["per_query_tokens"],
                    recall=stats["fact_recall_rate"],
                    success=stats["task_success_mean"],
                    saving=stats["saving_rate_pct"],
                )
            )
        lines.extend(
            [
                "",
                "## Interpretation",
                "",
                "- Token savings without fact recall and task success are not an improvement.",
                "- Negative saving rates mean the strategy used more tokens than full history.",
                "- Pre-processing costs (summary generation, memory population) are counted.",
                "- `task_success` is `None` unless human labels were supplied.",
                "",
            ]
        )
        return "\n".join(lines)


async def run_benchmark(
    *,
    settings: Settings,
    dataset: Dataset | None = None,
    strategies: tuple[str, ...] = STRATEGIES,
    runs: int = 1,
    results_dir: Path | None = None,
    provider: BaseProvider | None = None,
    labels: dict[str, int] | None = None,
    seed: int = 1234,
) -> dict[str, Any]:
    """Convenience wrapper used by the CLIs and tests."""
    loaded = dataset or load_dataset()
    runner = BenchmarkRunner(settings, provider=provider, results_dir=results_dir, seed=seed)
    return await runner.run(loaded, strategies=strategies, runs=runs, labels=labels)


__all__ = [
    "STRATEGIES",
    "BenchmarkRunner",
    "CaseResult",
    "ContextEchoProvider",
    "StrategyAggregate",
    "run_benchmark",
]
