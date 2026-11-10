# Benchmarks

## 1. What is compared

Three context strategies over the same cases, the same system prompt and the same context
scaffold — only the memory content differs:

| Strategy | Context content |
| --- | --- |
| `full_history` | the entire case transcript |
| `summary` | a pre-generated summary of that transcript |
| `nanomemory` | Working Memory + memories selected by the retriever under the token budget |

## 2. Fairness rules

1. Identical tasks, identical system prompt (`nanomemory.core.context_builder.SYSTEM_PROMPT`),
   identical XML scaffold; tool schemas are excluded from all three so they cannot inflate one
   strategy.
2. Fixed seed and the dataset SHA-256 recorded in every artifact.
3. Pre-processing cost is counted separately **and** in
   `total_tokens_with_preprocessing`:
   - summary strategy: estimated cost of producing each summary (history + summary tokens),
   - nanomemory: estimated cost of populating the memory store with the case facts,
   - full history: zero by definition.
4. `Token Saving Rate = (full_history_input_tokens − strategy_input_tokens) /
   full_history_input_tokens × 100 %`; a zero denominator yields `null`, and negative values
   are reported rather than clamped.
5. `task_success` uses the fixed rubric 0 = failed, 1 = partial, 2 = success and stays `null`
   unless a human label file (`--labels labels.json`) is provided. No LLM judge is used for
   fact recall.
6. Mock results are labelled `mock` and must not be quoted as real API token or cost results.

## 3. Metrics

| Metric | How it is produced |
| --- | --- |
| Input/Output/Total tokens | `usage_source=exact` from the provider, otherwise the named local estimator |
| Estimated API cost | price table only; otherwise `N/A` |
| Latency | wall-clock per provider call |
| Fact recall | deterministic NFKC + casefold substring match against `match_terms` |
| Forbidden claims | forbidden phrase present in the answer |
| Task success | human labels only |
| Context budget violation rate | cases whose token usage exceeded the request budget |
| Token saving rate | formula above |

The estimator is `heuristic-cjk1-latin4-v1`: one token per CJK character, one per four other
characters, rounded up. It is conservative and clearly labelled as an estimate.

## 4. Dataset

`benchmarks/datasets/memory_bench.json` (short transcripts) and
`memory_bench_long.json` (the same ten questions embedded in ~6,000-character transcripts).
Schema:

```json
{"dataset_schema_version": 1, "name": "…",
 "cases": [{"id": "case-001", "task": "…",
            "history": ["user: …", "assistant: …"],
            "summary": "…", "facts": ["…"],
            "required_facts": [{"id": "f1", "text": "…", "match_terms": ["windows 11"]}],
            "forbidden_claims": ["…"], "expected_outcome": "…", "max_tokens": 900}]}
```

## 5. Running

```bash
python -m benchmarks.token_benchmark --dataset benchmarks/datasets/memory_bench_long.json
python -m benchmarks.task_benchmark  --dataset benchmarks/datasets/memory_bench_long.json
```

Artifacts per run (`benchmarks/results/<run_id>/`):

- `cases.json` – exact cases used + dataset hash,
- `raw_results.jsonl` – one line per (case, strategy, run),
- `summary.json` / `summary.csv` – aggregates + metadata,
- `report.md` – human readable report,
- a copy at `benchmarks/results/<run_id>_summary.json` for the dashboard.

## 6. Results

Mock mode, `memory_bench_long.json`, 1 run, seed 1234. The provider echoes the context, so
fact recall here measures **context coverage**, not model capability.

| Strategy | Total tokens | Pre-processing | Per query | Fact recall | Saving vs full history |
| --- | --- | --- | --- | --- | --- |
| full_history | 31,964 | 0 | 3,196 | 1.00 | 0 % |
| summary | 2,158 | 15,647 | 216 | 0.86 | 93.25 % |
| nanomemory | 10,532 | 564 | 1,053 | 0.39 | 67.05 % |

Interpretation:

- The summary strategy is cheap per query only because summarisation is paid once; its
  total cost (17,805 tokens) is about 55 % of full history on this dataset, not 7 %.
- NanoMemory cuts per-query tokens by 67 % but recalls only 39 % of required facts with the
  keyword retriever and an 8-item cap. **That is a real quality regression.** It is reported
  rather than hidden, and it is the main reason the retriever roadmap item exists.
- Full history is cheap here (about 3,200 tokens per query) because the transcripts are
  ~6,000 characters. Longer real sessions make the trade-off more favourable to memory
  strategies; this dataset does not claim otherwise.

## 7. Real-API runs

Set `NANOMEMORY_PROVIDER=openai_compatible`, `NANOMEMORY_API_KEY`, `NANOMEMORY_BASE_URL` and
`NANOMEMORY_MODEL`, then rerun the CLI. Files are named `real-<timestamp>-<commit>*` and
`usage_source` becomes `exact` when the endpoint returns a usage block. Real results are not
copied into the README automatically.

## 8. Limitations

- Ten cases only: too few for statistical claims. Confidence intervals are not reported.
- The mock provider cannot measure answer quality; real mode with human labels is required
  for that.
- The dataset is about NanoMemory itself, which makes facts easy to retrieve lexically; a
  broader corpus would likely lower recall further.
