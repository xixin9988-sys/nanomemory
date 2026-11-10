# NanoMemory Benchmark Report

- run: `mock-20261110T122543Z-802e32d`
- mode: **mock** (mock-context-echo / context-echo)
- dataset: `nanomemory-core-facts-long-history` sha256 `aed258ddf03b6022f8601c43c8897f41c28ca03d7ecd95eb1928832cfa9c1698`
- cases: 10 · runs per case: 1 · seed 1234
- commit: `802e32d` · estimator: `heuristic-cjk1-latin4-v1`
- generated: 2026-11-10T12:25:43.355260+00:00

> context_recall_proxy (mock mode measures whether the context contained the facts; it is not a model-capability or real-API measurement)

| strategy | total tokens | preprocessing | per query | fact recall | task success | saving % |
| --- | --- | --- | --- | --- | --- | --- |
| full_history | 31964 | 0 | 3196.4 | 1.0 | None | 0.0 |
| summary | 2158 | 15647 | 215.8 | 0.8571 | None | 93.25 |
| nanomemory | 10650 | 564 | 1065.0 | 0.8571 | None | 66.68 |

## Interpretation

- Token savings without fact recall and task success are not an improvement.
- Negative saving rates mean the strategy used more tokens than full history.
- Pre-processing costs (summary generation, memory population) are counted.
- `task_success` is `None` unless human labels were supplied.
