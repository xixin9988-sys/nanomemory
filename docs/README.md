# Documentation map

| Area | Document |
| --- | --- |
| Product | [Requirements v1](product/PRODUCT_REQUIREMENTS_V1.md) · [Roadmap / status](product/ROADMAP_V1.md) |
| Architecture | [Vector architecture](internal/VECTOR_ARCHITECTURE.md) · [State machine](internal/STATE_MACHINE.md) |
| Decisions | [ADR-0001 SQLite first](adr/0001-sqlite-first-vector-store.md) · [ADR-0002 embedding profiles](adr/0002-embedding-profile-versioning.md) |
| API | [Vector API guide](guides/VECTOR_API.md) · [Scope brief](guides/SCOPE_BRIEF.md) |
| Security | [Security model](SECURITY.md) · [Data classification + trace policy](security/DATA_CLASSIFICATION_AND_TRACE_POLICY.md) |
| Quality | [Evaluation method](internal/EVALUATION.md) · [Release gates](release/RELEASE_GATES_V1.md) |
| Operations | [Backup / verify / restore](operations/BACKUP_RESTORE.md) |

Conventions:

- Decisions live in `adr/` and are dated; a superseded decision stays in the tree.
- Status documents link to the command or test that proves the claim.
- "Measured" means a report in `evals/reports/` produced by a committed script.
- The offline hashing provider is a lexical baseline, never a semantic quality claim.
