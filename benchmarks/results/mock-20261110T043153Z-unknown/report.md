# NanoMemory Benchmark Report

- run: `mock-20261110T043153Z-unknown`
- mode: **mock** (mock-context-echo / context-echo)
- dataset: `nanomemory-core-facts` sha256 `eef638139c4a463947e8ae7b4e4e3d8fbb7b9e96b72e3b565c88c641b892358e`
- cases: 10 · runs per case: 1 · seed 1234
- commit: `unknown` · estimator: `heuristic-cjk1-latin4-v1`
- generated: 2026-11-10T04:31:53.439586+00:00

> context_recall_proxy (mock mode measures whether the context contained the facts; it is not a model-capability or real-API measurement)

| strategy | total tokens | preprocessing | per query | fact recall | task success | saving % |
| --- | --- | --- | --- | --- | --- | --- |
| full_history | 4110 | 0 | 411.0 | 1.0 | None | 0.0 |
| summary | 2158 | 1720 | 215.8 | 0.8571 | None | 47.49 |
| nanomemory | 10532 | 564 | 1053.2 | 0.3929 | None | -156.25 |

## Interpretation

- Token savings without fact recall and task success are not an improvement.
- Negative saving rates mean the strategy used more tokens than full history.
- Pre-processing costs (summary generation, memory population) are counted.
- `task_success` is `None` unless human labels were supplied.
