# NanoMemory v1 — Product Requirements

Status: **baseline for v1.0**, frozen 2026-11-27. Supersedes the "local single-user MVP" framing
in older README text. Implementation status is tracked in each section; unreleased items are
marked **[not implemented]**.

## 1. Problem

LLM applications forget. The usual workaround — stuffing chat history into the prompt — is
expensive, unauditable and unsafe: it mixes user facts with model guesses and gives no way to
answer "why did you say that?" or "who else can see this?".

NanoMemory is an **auditable long-term memory engine**: memories are durable typed objects with
provenance, owner scope, lifecycle and version history; retrieval returns evidence plus a refusal
decision, not a plausible paragraph.

## 2. Target users

| Persona | Need | Success looks like |
| --- | --- | --- |
| AI application developer | add durable memory without building a vector stack | install, write, search, read a trace in under 10 minutes |
| Small SaaS team | per-tenant isolation and cost control | two tenants can never see each other's rows; daily budget is enforced |
| Self-hosting / regulated team | data stays on their infrastructure, every call auditable | backup/restore drill passes; trace does not store raw prompts by default |

## 3. In scope for v1.0

- Single-node deployment, SQLite vector store, Docker Compose.
- Python SDK, REST API, MCP server, CLI (same service rules for all entry points).
- Embedding providers: offline hashing (dev/test) and OpenAI-compatible (production).
- Hybrid retrieval with per-signal explainability and a calibrated refusal decision.
- Lifecycle: pending embedding, active, archived, soft-deleted, rolled back; version history.
- Tenant/user/ACL pre-filtering enforced in SQL before similarity.
- Signed tenant context (HMAC, timestamp window) and a production requirement for the key.
- Traces with content minimisation by default; feedback loop; badcase export.
- Backup/verify/restore tooling and a documented drill.
- Evaluation harness with an adversarial share and a CI gate.

## 4. Non-goals (v1.0)

- A general-purpose vector database or an ANN benchmark competitor.
- Multi-node / active-active deployment, Kubernetes operator, managed SaaS.
- PostgreSQL + pgvector (trigger conditions in ADR-0001).
- Model training, fine-tuning or automatic fact extraction without review.
- Knowledge-graph product features.
- Answer generation: NanoMemory returns evidence and a refusal decision; grounded answer checks
  belong to mem-guard.

## 5. User stories and acceptance criteria

### US-01 Persistent write

1. **Given** a valid signed context, **when** a client writes a memory, **then** the response has
   a unique id, content hash, embedding model/dimension and a trace id.
2. **Given** the embedding provider times out, **when** the write fails, **then** the record is
   stored as `pending_embedding`, is not retrievable, and the API returns a retryable 503 with
   its id.
3. **Given** the same `Idempotency-Key` is replayed, **when** the second request arrives,
   **then** the same record is returned and no second embedding call is made.

### US-02 Scoped retrieval

1. **Given** two tenants with identical content, **when** tenant A searches, **then** tenant B's
   rows are not in the candidate set, the results or the prompt context.
2. **Given** a caller without an ACL tag, **when** it searches, **then** rows shared only with
   that tag are excluded by SQL before similarity.
3. **Given** a missing, expired or tampered signature, **when** a search arrives, **then** the
   request fails with 401 and no unscoped query runs.

### US-03 Refusal

1. **Given** the corpus has no answer, **when** the user asks, **then** the response has
   `insufficient_context=true` and empty `items`.
2. **Given** a similar memory without the asked entity/amount/date, **when** the user asks about
   that entity, **then** retrieval must not present the similar row as the answer (the
   answer-level guard is mem-guard's responsibility and is evaluated separately).
3. **Given** the embedding provider is down but BM25 is available, **when** a search runs,
   **then** the response is `degraded=true`, returns lexical matches, and still applies the
   refusal floor.

### US-04 Versions, rollback, deletion

1. **Given** version 1 exists, **when** the client patches with `expected_version=1`, **then**
   version 2 is created and version 1 stays readable.
2. **Given** a concurrent update already produced version 2, **when** a stale patch arrives,
   **then** the API returns 409 and does not overwrite.
3. **Given** a rollback to version 1, **when** it succeeds, **then** the record is
   `rolled_back`, is not retrievable, and only an explicit reactivation makes it retrievable.

### US-05 Auditability and privacy

1. **Given** a successful search, **when** an authorised operator reads the trace, **then** it
   shows the filter shape, candidate/returned ids, per-signal scores, model and timings.
2. **Given** minimal trace policy (default), **when** a trace is written, **then** raw query,
   prompt context and model response are not persisted; hashes and lengths are.
3. **Given** a 👎 feedback, **when** it is reviewed, **then** it can become a redacted eval case
   and never mutates memory or triggers training automatically.

### US-06 Install and recovery

1. **Given** a clean supported environment, **when** the operator follows the README, **then**
   they can write, search, inspect a trace and create a backup.
2. **Given** a backup with manifest, **when** it is restored in an isolated environment,
   **then** hash, integrity and record counts verify.
3. **Given** a failed database migration, **when** the service starts, **then** it refuses
   traffic and the old file stays recoverable.

## 6. Success metrics

v1.0 release gates live in [RELEASE_GATES_V1.md](../release/RELEASE_GATES_V1.md). Current offline
baseline (hashing provider, 44 cases) is **not** a production quality claim:

| Metric | Offline baseline | v1 gate | Status |
| --- | ---: | ---: | --- |
| Recall@5 | 0.871 | ≥ 0.90 on a real provider, frozen set | not met (provider pending) |
| Refusal accuracy | 1.000 (5 cases) | ≥ 0.95, ≥ 30 refusal cases | not met (sample size) |
| Stale violations | 0 | 0 | met (offline) |
| Cross-tenant leaks | 0 (tested) | 0 | met |
| p95 search latency | 1.9 ms local | < 200 ms single node at target corpus | not measured at load |
| Time to first recall | — | ≤ 10 minutes from clean install | not measured |

## 7. Compatibility policy

- REST is versioned under `/api/v1`; breaking changes require a new major version.
- Every write accepts `Idempotency-Key`; every update accepts `expected_version`.
- Scope is never a request-body field.
- Error responses carry a stable `code`, a safe `message` and an optional `details`.
- SDK/CLI/MCP call the same service; no entry point may widen the filter.
- Trace schema and evaluation dataset are versioned; old traces stay readable after upgrades.

## 8. Open decisions

| # | Decision | Owner | Needed by |
| --- | --- | --- | --- |
| D1 | Production embedding provider + region + price | product | before v1 gate run |
| D2 | Free/Team SKU limits and support boundary | product | before first paid pilot |
| D3 | Trace raw-content retention policy per customer | security | before onboarding |
| D4 | pgvector trigger thresholds (rows, p95, replicas) | engineering | ADR-0001 review |

