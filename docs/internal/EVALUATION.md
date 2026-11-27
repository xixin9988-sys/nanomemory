# Evaluation: method, baseline and limits

The eval set is the product's regression harness. It is versioned in `evals/dataset.json`,
seeded through the public service API, and scored by `evals/run_eval.py`. No number in the
README is hand-written: every claim links to a report under `evals/reports/`.

## Dataset

| Property | Value |
| --- | --- |
| name | `nanomemory-support-v1` |
| corpus | 24 memories, bilingual customer-support domain, one intentionally archived |
| cases | 44: 31 recall, 5 refusal, 1 stale, 7 answer-grounding |
| adversarial share | 13 / 44 = **29.5 %** (refusal + stale + grounding) |
| difficulty labels | `lexical` (29) and `semantic` (2) |

Case types:

- **recall** — expected memory id must appear in top-K; forbidden ids must not.
- **refusal** — no memory answers the question; the API must return
  `insufficient_context=true` and no items.
- **stale** — an archived/superseded memory must never be served as current.
- **grounding** — retrieval legitimately returns a topically similar memory, but the answer must
  not fabricate the missing entity (A1001 vs C9999, 11.11 vs 12.12, support line vs customer
  phone). These are measured by mem-guard's grounded-answer guard, not by retrieval, and are
  reported separately instead of being gamed into the recall metric.

## Metrics

| Metric | Definition | Gate |
| --- | --- | --- |
| Recall@K | share of recall cases with an expected id in top-K | ≥ 0.85 |
| MRR | mean reciprocal rank of the first expected id | reported |
| Refusal accuracy | share of refusal cases answered with an empty, flagged result | ≥ 0.75 |
| Stale violations | archived/forbidden ids appearing in results | == 0 |
| p95 latency | wall time per search | reported |

## Baseline (hashing provider, offline, K=5)

| Metric | Value |
| --- | --- |
| Recall@5 | **0.871** |
| MRR | 0.839 |
| Refusal accuracy | **1.000** |
| Stale violations | **0** |
| Pass rate | 0.892 (33/37 scored cases) |
| Lexical subset recall | 0.931 (27/29) |
| Semantic subset recall | 0.000 (0/2) |
| Latency mean / p95 | 1.46 ms / 1.93 ms |

Four recall misses, all understood:

| Case | Why it fails with hashing |
| --- | --- |
| c01 我的显示偏好是什么？ | "显示偏好" shares almost no characters with the stored 深色模式 sentence; signal 0.093 is below the calibrated floor |
| c27 我的蓝牙耳塞有质量问题 | 耳塞 vs 耳机 is a synonym, not a substring; lexical features cannot bridge it |
| c28 包裹一直没到，能查一下物流吗 | no order id and no shared noun phrase with the two order rows |
| c30 退款多久能到账 | asks for a date the corpus does not state; retrieval surfaces the refund policy instead of the specific refund |

The semantic failures are the honest cost of the offline provider. With a real embedding endpoint
the same dataset should improve; re-run and re-calibrate, do not edit the cases.

## The refusal floor is calibrated, not guessed

```bash
python evals/calibrate.py --provider hashing --dimension 384
```

The tool seeds once, queries every case with `allow_weak=True`, and sweeps
`NANOMEMORY_VECTOR_CONTENT_FLOOR` from 0.06 to 0.40, printing recall/refusal for each floor.
The shipped default (0.10) maximises refusal accuracy subject to recall ≥ 0.85 on this dataset.
On this corpus the pure-refusal cases top out at 0.098 and the weakest true positive sits at
0.093 — the classes are 0.005 apart, which is exactly why the floor is a setting and why a real
embedding model needs its own calibration run.

## Reproduce

```bash
python evals/run_eval.py --k 5 \
  --report evals/reports/baseline-hashing.md \
  --json   evals/reports/baseline-hashing.json

# production provider
NANOMEMORY_EMBEDDING_API_KEY=... python evals/run_eval.py \
  --provider openai_compatible --dimension 1536 --report evals/reports/openai.md
```

Exit code is non-zero when a gate fails, so CI can enforce it.

## Limits (stated, not hidden)

1. Retrieval-only eval cannot measure answer groundedness; that is why the 7 grounding cases are
   delegated to mem-guard and reported as `N/A` rather than counted as passes.
2. 44 cases is enough to catch regressions, not enough to claim statistical significance. Add
   real badcases from `GET /api/v1/feedback/export` as they arrive.
3. Latency is local SQLite with warm caches; it is a regression signal, not a capacity plan.
4. The dataset is written by the same team that wrote the code. The adversarial share and the
   forbidden-id checks are the guard against grading one's own homework; independent cases should
   be added before any external quality claim.
