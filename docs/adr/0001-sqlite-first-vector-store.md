# ADR-0001 — SQLite is the v1 vector backend

- **Status:** Accepted (2026-11-27)
- **Supersedes:** the pgvector-first proposal in PR #2 ("Vector Memory v1 / pgvector ADR")
- **Related:** Issue #3 (PostgreSQL + pgvector Repository) is re-scoped to milestone M6

## Context

NanoMemory v0.3 already ships a working SQLite vector engine: transactional versions, SQL-level
tenant pre-filtering, traces, feedback, migration and a measured eval set. The competing proposal
argued that PostgreSQL + pgvector must be the production system of record for v1.

Two facts decide the question:

1. The first paid product is **self-hosted single-node**. A database server is an operational
   dependency that the target user (a developer or small team) must install, back up and secure.
2. There is no load evidence that SQLite is the current bottleneck. The design target is roughly
   5,000 memories per tenant; a filtered brute-force cosine scan at that size is milliseconds.

## Decision

For v1.0:

- **SQLite is the only supported vector backend.** It is a deliberate product choice, not a
  temporary shortcut.
- The service depends on a `VectorMemoryRepository` Protocol. SQLite is adapter #1 and must pass
  the shared contract tests (`tests/contract/test_vector_repository.py`).
- pgvector is **not** implemented in v1. It remains a future adapter behind the same contract.

## Consequences

Positive: one process, one file, trivial backup (SQLite online backup API), no server to secure,
fast local retrieval, simple self-hosted story.

Negative: single writer; no horizontal scale; no row-level security inside the database; large
corpora need an ANN backend; the contract must be strict enough that a future adapter cannot
quietly change isolation semantics.

Mitigations: WAL + one writer lock; candidate cap; contract tests; backup/restore drill; the
service never talks to SQL directly.

## Trigger conditions for starting M6 (pgvector)

Start the adapter only when at least one is true, measured — not assumed:

1. A tenant exceeds ~5,000 active memories **and** p95 retrieval exceeds 200 ms on target
   hardware with a warm cache.
2. The deployment needs more than one API replica sharing a database.
3. A customer contract requires PostgreSQL operations (managed backups, RLS, existing DBA).
4. A managed-cloud offering is approved.

When triggered, the pgvector adapter must pass the same repository contract plus new isolation
tests: HNSW/IVFFlat filtering must be proven with `EXPLAIN` plans and cross-tenant canaries;
global ANN followed by Python post-filtering is forbidden.

## Notes on the PR #2 conflict

PR #2 cannot be merged as written. Its useful parts (profile versioning, repository abstraction,
eval discipline) are captured in ADR-0002 and the M1/M2 work items; its "pgvector is the v1 system
of record" premise is rejected by this ADR.
