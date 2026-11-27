# Release gates — NanoMemory v1.0

A release is blocked when any **blocking** row is red. "Offline baseline" values come from
`evals/reports/baseline-hashing.md` and are not production quality claims.

## 1. Quality gates

| # | Gate | Threshold | Measurement | Current (offline) | Status |
| --- | --- | --- | --- | --- | --- |
| Q1 | Recall@5 | ≥ 0.90 | frozen answerable set, real embedding profile | 0.871 | **not met** (hashing; real provider pending) |
| Q2 | Refusal accuracy | ≥ 0.95 with ≥ 30 refusal cases | adversarial set | 1.000 with 5 cases | **not met** (sample size) |
| Q3 | Stale / forbidden recall | 0 | forbidden ids in top-K | 0 | met |
| Q4 | Cross-tenant / unauthorized hits | 0 | canary suite + contract tests | 0 | met |
| Q5 | Unsupported answer rate | ≤ 2% | mem-guard grounding eval | not measured here | **not met** (external) |
| Q6 | Schema validity | 100% | API/SDK/MCP contract tests | 100% | met |
| Q7 | Migration parity | 100% | v1→v2 dry-run + restore drill | dry-run tool exists; parity report pending | **partial** |

A gate is only "met" when the command that produces it is in CI and the report is linked. Manual
claims do not count.

## 2. Engineering gates

| # | Gate | Command |
| --- | --- | --- |
| E1 | Lint + format | `ruff check .` / `ruff format --check .` |
| E2 | Unit + integration tests | `pytest -q` |
| E3 | Repository contract | `pytest tests/contract -q` |
| E4 | Eval regression | `python evals/run_eval.py --k 5 --min-recall 0.85 --min-refusal 0.80` |
| E5 | Security scan | secret scan + dependency audit (CI) |
| E6 | Backup/restore drill | `python scripts/vector_backup.py ...` + restore in a temp dir |

## 3. Security gates

| # | Gate | Evidence |
| --- | --- | --- |
| S1 | Signed context required in production | settings refuse to boot without key |
| S2 | Body scope fields rejected | 422 tests |
| S3 | Cross-tenant canary | contract test with identical vectors |
| S4 | Trace content minimisation | default `minimal`; test asserts no raw query |
| S5 | Secret/PII before embedding | gateway tests (mem-guard) |

## 4. Release checklist

1. CI green on the release commit (all E gates).
2. Q gates evaluated on the frozen dataset with the production profile; report committed.
3. Backup created, restored into a fresh directory, counts and hash verified.
4. Changelog and known-limitations section updated.
5. Rollback path stated (previous image + compatible DB snapshot).
6. No "met" gate without a link to its evidence.
