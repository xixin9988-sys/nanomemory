# Roadmap v1.0 (implementation status)

Source: the product owner's *NanoMemory Product Blueprint v1.0* (2026-11-27). The original file
was a planning document on the owner's machine and is not stored in this repository; this page is
the engineering status derived from it, with links to the artefacts that prove each claim.

Execution order is fixed: **M0 → M1 → M2 → M3 → M4 → M5**, with M6 opened only by evidence.

| Milestone | Goal | Status | Evidence |
| --- | --- | --- | --- |
| **M0** Product/document baseline | remove conflicts between docs and code; freeze scope | **done** | [PRD](PRODUCT_REQUIREMENTS_V1.md), [ADR-0001](../adr/0001-sqlite-first-vector-store.md), [ADR-0002](../adr/0002-embedding-profile-versioning.md), [trace policy](../security/DATA_CLASSIFICATION_AND_TRACE_POLICY.md), [release gates](../release/RELEASE_GATES_V1.md) |
| **M1** Core stabilisation | repository boundary, isolation, privacy, recovery | **in progress** | repository contract + tests, trace minimisation + pruning, backup/verify/restore + runbook; see gaps below |
| **M2** Real semantic quality | prove a real embedding provider improves semantics; freeze the eval | **not started** | needs provider decision (region, price, model) |
| **M3** Install / SDK / MCP / DX | a developer can install and integrate without maintainer help | **not started** | CI exists; doctor, examples, install smoke pending |
| **M4** mem-guard as governance layer | typed policy, filter plan, fail-closed, grounding | **partial** | gateway exists (mem-guard v2.0); typed contracts and fail-closed policy compiler pending |
| **M5** OmniCare pilot | one real support/order loop on top of M1–M4 | **not started** | depends on M2–M4 |
| **M6** pgvector decision | scale-out only when measured triggers fire | **deferred by design** | triggers in ADR-0001 |

## M1 remaining work

1. **Embedding profile persistence** — add `profile_id` to records, traces and filters; shadow
   rebuild + parity check before switching (ADR-0002). Today model+dimension is the guard.
2. **Signed-context claims** — audience, issuer, key id, nonce/replay window and key rotation;
   today the envelope has tenant/user/roles/ACL/timestamp only.
3. **Schema migration tests** — upgrade from a v1 vector database, fail safely on a newer schema,
   prove the old file stays recoverable.
4. **Tenant-scope audit** — every read/update/delete/trace path exercised by a negative test.
5. **Load test** — candidate count, p95 at the 5,000-memory design point, SQLite busy behaviour.

## M2 entry criteria

- M1 profile work merged, so eval numbers are tied to a named profile.
- 50+ labelled cases (current set: 44) with semantic holdout and ≥30 refusal cases.
- A real provider configuration and budget approved by the owner.

## M4/M5 contract note

The cross-repo contract (signed context + `RetrievalFilterPlan` + `GovernanceDecision`) is
frozen in mem-guard's typed contracts before OmniCare integrates. mem-guard must never widen the
scope NanoMemory already allows; NanoMemory re-validates every plan it receives.

## PR #2

[ADR-0001](../adr/0001-sqlite-first-vector-store.md) supersedes the pgvector-first premise of PR
#2. The PR should be rewritten as "SQLite-first + Repository abstraction" or closed with a link to
the ADR; it must not be merged as-is.
