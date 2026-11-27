# NanoMemory Vector Memory v1 — Product Brief

Status: **proposal for owner approval; not yet signed off**  
Product owner: repository owner  
Technical owner: repository maintainer  
Target: first commercially supportable NanoMemory release  
Baseline inspected: current main branch, 2026-11-27

## 1. Product statement

NanoMemory is a model-agnostic memory service and SDK that stores auditable memory records and retrieves relevant memories using semantic vectors, lexical signals, relationships, trust and lifecycle metadata. It is not merely a vector database wrapper and it is not a Markdown notebook.

**Core promise:** an application can remember, retrieve, explain, update, export and forget information without silently inventing facts or exposing information outside the caller's access boundary.

Vector-first means vectors are the primary semantic retrieval index. It does **not** mean deleting the source text. Original content, provenance, state and permissions remain the canonical evidence from which embeddings can be regenerated and retrieval decisions can be explained.

## 2. Target users and primary journeys

Primary users:
- Developers integrating memory into agents, assistants and business applications through Python SDK, REST and MCP.
- Small teams that need a self-hosted memory service with auditable provenance and tenant-aware access controls.
- Application owners who need measurable retrieval quality and an explicit abstention path.

Primary journeys:
1. Install and configure a NanoMemory service with a selected embedding profile.
2. Authenticate a trusted application and create a tenant-scoped memory record.
3. Form a memory from text or a conversation, retaining source, evidence and review state.
4. Recall relevant memories using semantic + lexical + relationship signals under a token budget.
5. Inspect why a memory was stored, selected, filtered or rejected.
6. Correct, supersede, archive, restore, export or delete data with an auditable state transition.
7. Evaluate retrieval quality and operate the service using health, metrics, logs, backup and restore.

## 3. Explicit scope for v1

In scope:
- PostgreSQL as the production system of record, with the pgvector extension for dense vectors.
- SQLAlchemy/asyncpg repository implementation and Alembic schema migrations.
- A typed embedding-provider interface, initially supporting one configured active embedding profile per deployment and at least one local and one remote provider adapter.
- Persisted source text, normalized metadata, provenance, lifecycle state, ownership, access scope, embedding profile/version and vector.
- Hybrid retrieval: dense vector candidates, lexical candidates, optional relation expansion, reciprocal-rank fusion, explainable reranking and token-budget packing.
- Server-side identity context and physical security-domain partitioning so only authorized partitions participate in ANN retrieval; PostgreSQL row-level security as defence in depth.
- Idempotent import from the existing JSON long-term memory format, preserving IDs, provenance, versions and status.
- SDK, REST and MCP contracts that expose stable remember, recall, explain, update, archive/restore, export and health operations.
- A 50-case minimum labelled retrieval evaluation set, including unanswerable questions.
- Structured trace records, configurable retention, health checks, usage/cost metering, backup and restore.
- Docker Compose deployment for a documented single-node production-shaped installation.

## 4. Non-goals for v1

- Replacing PostgreSQL or claiming to be a general-purpose vector database.
- Building a proprietary embedding model or training foundation models.
- Kubernetes operator, multi-region active-active, sharding or distributed vector indexing.
- Supporting arbitrary embedding dimensions in one live index. A deployment uses one active embedding profile/dimension; changing it uses a versioned blue-green re-embedding migration.
- Autonomous memory writes with no provenance, validation or admission gate.
- Treating model-generated confidence as proof that a statement is true.
- Supporting every model provider, every vector database and every agent framework in the first commercial release.
- Building a public hosted shared multi-tenant SaaS control plane before the self-hosted production baseline is secure and reproducible.
- Sharing one global HNSW index across mutually isolated customers or users.

## 5. Product and data principles

1. **Evidence before belief:** source text and provenance are retained; embeddings are derived, disposable indexes.
2. **Default deny:** identity and access scope are resolved before retrieval. Missing or unverifiable identity must not become a broad search. ANN candidates are searched only inside physical security domains that match the caller's resolved grants.
3. **No silent loss:** failed writes, migration errors, capacity limits and corrupt records return explicit errors.
4. **Versioned change:** records, schemas, embedding profiles and policies are versioned.
5. **Reversible lifecycle:** archive, restore, supersede and rollback are explicit operations with audit events.
6. **Measured retrieval:** release claims require labelled evaluation artifacts, not a single mock benchmark.
7. **Provider independence:** embedding and LLM providers are adapters; the core domain model does not depend on a vendor SDK.
8. **Privacy by default:** do not log raw prompt or memory content by default. Content-level traces require explicit opt-in, access control and retention policy.

## 6. Canonical memory lifecycle

States:
- candidate: extracted but not admitted.
- pending_confirmation: model-derived or low-evidence content awaiting confirmation.
- active: admitted and retrievable within authorization scope.
- conflict: conflicting or duplicate candidate requiring resolution.
- superseded: retained historical record replaced by a newer record.
- archived: not returned by normal recall, but retained per retention policy.
- quarantined: untrusted, malformed or suspicious content excluded from agent retrieval.
- rejected: admission failed; the decision and reason are retained in the audit trail where policy permits.

Allowed transitions:
- candidate -> pending_confirmation | active | quarantined | rejected
- pending_confirmation -> active | quarantined | rejected
- active -> superseded | conflict | archived | quarantined
- conflict -> active | superseded | archived | quarantined
- superseded -> active only through an explicit rollback operation with a reason and successor handling
- archived -> active only through an explicit restore operation after authorization
- quarantined -> pending_confirmation | rejected; never directly to active without revalidation
- rejected is terminal; create a new candidate rather than mutate the rejected decision

Every transition is atomic, version-checked, attributable and append-only in the audit log. A rollback never erases the transition it compensates. Embedding refresh failure must not roll back or destroy the canonical memory record; the record remains stored and is marked as needing re-indexing.

## 7. Retrieval contract

For every recall request:
1. Resolve authenticated tenant, subject, roles, scopes and collection restrictions from trusted server context.
2. Compile an immutable authorization filter and resolve the exact permitted security-domain partitions.
3. Use partition pruning to search only authorized security domains. Apply tenant, owner, collection, status and trust predicates in SQL, with RLS as defence in depth. Do not rely on a shared global HNSW index plus post-scan WHERE filtering to provide ANN isolation.
4. Generate the query embedding using the active profile.
5. Retrieve dense and lexical candidates independently.
6. Fuse ranks, optionally rerank, explain each score component and pack under a hard token budget.
7. Return an explicit insufficient-context result when the evidence floor is not met.

The service must fail closed if the repository cannot prove the authorized partition set is used. pgvector approximate-index filtering can occur after index scanning; a global shared HNSW index can therefore affect filtered recall and performance across tenants even when unauthorized rows are not returned. If a request has finer-grained ACLs than its physical security domain, use an exact filtered scan or deny it. Post-filtering unauthorized rows in Python is not an acceptable security boundary.

A response must include: request ID, query/profile version, retrieved IDs, score breakdown, filter/policy version, selected content references, token estimate, latency breakdown and insufficient-context reason. Raw content is excluded from operational logs unless content tracing has been explicitly enabled.

## 8. Evaluation and proposed release gates

Create a versioned, human-labelled evaluation set with at least 50 questions:
- factual recall and paraphrases;
- preference and profile recall;
- temporal and superseded facts;
- multilingual / Chinese-English queries;
- relation and multi-hop cases;
- conflicting and low-confidence memories;
- absent-knowledge questions requiring abstention;
- unauthorized cross-user and cross-tenant canary cases.

Proposed targets; these are gates to validate and approve, not claims about current performance:
- Recall@10 >= 0.90 on answerable cases.
- nDCG@10 >= 0.85 on ranked relevance labels.
- Unauthorized retrievals = 0 in adversarial tenant/owner/collection tests.
- EXPLAIN-based tests prove only authorized security-domain partitions are scanned for ANN requests.
- Correct abstention >= 0.95 on unanswerable cases.
- Every retrieved item has a complete provenance reference and score explanation.
- No token-budget overflow in the evaluation suite.
- Repeated runs record dataset hash, code revision, embedding profile/version and configuration.

Report confidence intervals and per-category results. Do not optimize aggregate scores by hiding low-performing categories.

## 9. User stories and Given / When / Then acceptance

### US-01 — Store an auditable memory
- Given an authenticated application identity and valid source metadata, when it submits a memory, then NanoMemory stores the canonical text, owner/scope, provenance, status and a versioned embedding.
- Given an unauthenticated or unscoped request, when it attempts to create a memory, then the API rejects it without creating a record.
- Given an embedding provider timeout, when a valid memory is submitted, then the service returns a retryable indexing state or explicit failure without losing or duplicating the canonical record.

### US-02 — Retrieve semantically
- Given a stored memory expressed with different wording from the query, when an authorized caller recalls it, then semantic retrieval can return it with dense and fused score explanations.
- Given an authorized query with no sufficiently relevant result, when recall completes, then the API returns insufficient_context rather than fabricating a hit.
- Given a query budget, when candidate packing exceeds that budget, then lower-ranked candidates are excluded deterministically and the response reports the reason.

### US-03 — Enforce access before retrieval
- Given two tenants with similar or identical memory text, when tenant A queries, then no tenant B record may enter candidate results, reranking, traces or model context.
- Given a caller restricted to a collection, when it queries another collection, then the database query and RLS deny those rows before vector ranking.
- Given a missing policy or repository filter, when recall begins, then the service fails closed and emits a security audit event.

### US-04 — Correct and roll back a memory
- Given an active record and a verified correction, when a new version supersedes it, then the old version remains auditable and is excluded from default recall.
- Given an incorrect supersession, when an authorized operator requests rollback, then the previous record can be reactivated through an explicit compensating transition.
- Given a stale expected version, when a client updates a record, then the API returns a version conflict and does not overwrite newer data.

### US-05 — Migrate existing JSON data
- Given a valid legacy JSON store and an empty PostgreSQL target, when dry-run migration executes, then it reports counts, validation errors and hash mismatches without modifying source data.
- Given a successful dry run, when migration is applied, then IDs, provenance, lifecycle state and versions are preserved and every migrated record is reconciled.
- Given an interrupted migration, when it is retried or rolled back, then the operation is idempotent and the original JSON backup remains recoverable.

### US-06 — Explain and audit
- Given a retrieved memory, when a caller requests an explanation, then the service returns provenance, state history, embedding profile and retrieval score components allowed by that caller's permissions.
- Given a denied retrieval, when an authorized auditor inspects the trace, then the policy version and reason code are available without disclosing unauthorized content.
- Given content tracing is disabled, when normal requests execute, then logs contain identifiers and metrics but not raw prompts or memory bodies.

### US-07 — Operate the service
- Given PostgreSQL or the embedding provider is unavailable, when health is queried, then health reports degraded dependencies and the service does not report ready.
- Given an operator requests backup and restore, when manifests and hashes verify, then restore creates a pre-restore snapshot and records the outcome.
- Given a new release fails smoke tests, when deployment is promoted, then the release is blocked and the previous healthy version remains available.

## 10. Required UI and workflow states

Every UI surface and API workflow must define:
- loading / in progress;
- success with data;
- empty state / no matching data;
- validation error;
- dependency error / retryable failure;
- permission denied;
- stale version / conflict;
- maintenance / migration mode;
- partial indexing / re-index required;
- recovery required.

These states are required for dashboard, session management, memory review, import/export, backup/restore and evaluation runs. Do not treat an empty result as an error.

## 11. Commercial readiness checklist

- Security threat model and SECURITY.md.
- Secrets scan and dependency vulnerability scan in CI.
- No model or embedding API key in browser code.
- Per-tenant quotas, rate limits, usage records and cost visibility.
- Documented backup retention and a tested restore drill.
- Versioned API and migration compatibility policy.
- Installation guide, production guide, troubleshooting, CONTRIBUTING and support policy.
- License and third-party dependency inventory reviewed before commercial distribution.
- Privacy notice and data processing/retention controls for hosted use.

## 12. Approval

This brief is a proposal, not a signed approval. Before implementation is declared in scope, the repository owner must approve:
- the product scope and non-goals;
- the proposed release gates;
- the single active embedding profile per deployment;
- the security-domain partitioning and exact-search fallback policy;
- PostgreSQL + pgvector as the production system of record.

Approval record:
- Product owner: PENDING
- Technical owner: PENDING
- Date: PENDING
