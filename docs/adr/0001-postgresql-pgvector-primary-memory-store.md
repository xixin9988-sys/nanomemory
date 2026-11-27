# ADR-0001: PostgreSQL + pgvector as the production memory store

- Status: **Proposed**
- Date: 2026-11-27
- Owners: pending repository owner approval
- Scope: NanoMemory vector-memory v1

## Context

The current NanoMemory long-term memory implementation writes a JSON document (knowledge.json) and uses lexical / character-trigram scoring. The repository already has valuable validation, provenance, lifecycle, locking, backup, recovery and audit behaviour. The retrieval benchmark documents a material recall gap on its current small mock dataset.

The product requirement is to make vector retrieval central while keeping memory auditable, reversible and safe for multi-user application integration.

The uploaded Token Neural Space project is a local Transformer hidden-state and attention visualization lab. It computes PCA projections and cosine-similarity matrices for model internals, but it is not a persistent embedding store, does not implement nearest-neighbour retrieval or tenant authorization, and should not be used as NanoMemory's storage engine.

## Decision

Use PostgreSQL as the production system of record and pgvector for dense embedding storage and nearest-neighbour search.

Keep canonical memory records separate from derived embeddings:
- Canonical row: content, ownership, scope, type, trust, provenance, lifecycle, versions, timestamps and metadata.
- Embedding row/index: vector, provider, model, model revision, dimensions, normalization, input hash and indexing status.
- A memory remains valid and auditable if embedding generation is temporarily unavailable.
- Embeddings are rebuildable derivatives, never the only copy of a memory.

Use one active embedding profile and dimension per deployment for v1. Store the profile ID and revision on every embedding. Changing to a profile with a different dimension uses a blue-green migration: create a second index/table, backfill and evaluate, dual-read or shadow-compare, then atomically switch the active profile. Do not mix incompatible vector dimensions in one ANN index.

## Logical schema

### memory_records
- id (UUID or stable legacy ID)
- tenant_id
- security_domain_id (physical ANN isolation boundary: personal scope or shared collection/workspace scope)
- owner_user_id
- session_id (nullable)
- collection_id
- content (canonical source text)
- normalized_content_hash
- memory_type
- source_type
- trust_level
- status
- provenance (JSONB, schema-validated)
- metadata (JSONB, schema-validated and size-limited)
- importance, confidence
- record_version
- valid_from, valid_until, expires_at
- created_at, updated_at, archived_at

### memory_embeddings
- id
- memory_id (FK)
- tenant_id (for RLS and partition pruning)
- security_domain_id (same partition key as the canonical record)
- embedding_profile_id
- embedding_model
- embedding_revision
- dimension
- vector (fixed dimension for the active profile)
- source_content_hash
- indexed_at
- index_status
- unique key on memory_id + embedding_profile_id + source_content_hash

### memory_events
- append-only event ID, tenant, memory ID, actor, event type, from/to state, reason code, previous/new record version, timestamp and request ID.

### embedding_profiles
- profile ID, provider, model, revision, dimension, normalization, distance metric, tokenizer / preprocessing version, active flag and creation date.

The concrete physical schema and indexes must be validated against the chosen pgvector version before implementation. Current pgvector documents HNSW support for vector up to 2,000 dimensions and halfvec up to 4,000 dimensions; validate the selected provider dimension and index operator class at startup/migration time. Keep vector DDL behind an adapter so the pure domain layer does not import SQLAlchemy or pgvector classes.

## Security-domain partitioning

For v1, use PostgreSQL LIST partitioning keyed by a server-resolved security_domain_id (with tenant_id as a mandatory companion predicate). An access domain represents a single permission boundary, not merely a convenient grouping label. Private user memory and shared workspace/collection memory must not share the same ANN partition unless every member is authorized for every record in that partition. A caller allowed to multiple domains queries only those partitions and merges the resulting authorized candidate lists.

Start with a self-hosted / single-customer trust boundary per deployment. A hosted shared multi-tenant control plane is explicitly deferred until partition lifecycle, tenant placement, RLS ownership and filtered ANN recall are load-tested. For high tenant or partition counts, benchmark PostgreSQL planning cost and consider dedicated databases/shards. Do not claim that arbitrary row-level ACLs are ANN-prefiltered merely because they appear in WHERE or RLS: when an ACL is finer than a physical security domain, use an exact filtered scan or move the records to a correctly isolated domain.

HNSW is approximate. Use iterative scans where they improve the number of authorized results returned, and record filtered recall. Iterative scans improve candidate availability but are not a substitute for physical security-domain isolation.

## Retrieval design

1. Resolve tenant/user/collection authorization from verified server context, never from caller-controlled body fields.
2. Resolve the exact set of authorized security_domain_id values before retrieval. Each physical ANN partition contains only records that share the same access boundary (for example one user's private memories or one shared workspace collection).
3. Query only those partitions using a bound security_domain_id predicate. PostgreSQL partition pruning must select only authorized partitions; each partition has its own local HNSW index. Enable RLS as defence in depth and apply owner, status and trust predicates in SQL.
4. If a request's ACL cannot be represented by the selected physical security domains, use an exact filtered scan over authorized rows or deny the request. Never fall back to a global ANN scan followed by application-side filtering.
5. Run dense ANN retrieval with cosine distance (or a benchmark-justified metric) only inside the authorized partitions.
5. Run lexical retrieval against a language-aware lexical index. Keep tokenizer choice explicit; Chinese and mixed-language recall must be evaluated rather than assuming English FTS behaviour is adequate.
6. Optionally expand permitted related/superseding records.
7. Fuse candidate ranks using reciprocal-rank fusion (RRF); rerank using explainable signals and optional cross-encoder only after authorization filtering.
8. Pack results under the existing hard token and request budgets.
9. Return score components, candidate IDs, active embedding profile, policy version and insufficient-context reason.

Security invariant: no unauthorized record may be present in a candidate list, reranker input, prompt context, trace payload or response. pgvector documents that approximate-index WHERE filters are applied after the index scan and that vectors in a shared multitenant ANN index can affect other tenants' recall and speed. Therefore a shared global HNSW index plus RLS alone is not accepted as the isolation design. Partition pruning to authorized security domains is required before local ANN search; RLS remains defence in depth. The integration suite must inspect EXPLAIN plans and test both row isolation and partition pruning. If this cannot be proved, use exact filtered search or fail closed.

## Embedding provider contract

Define a provider protocol with:
- provider_id and model_id;
- model_revision and dimension;
- input normalization / preprocessing version;
- async embed_documents and embed_query methods;
- bounded batch size and explicit request timeout;
- retry classification (transient vs permanent);
- normalized float32 vector output;
- usage and cost metadata where available;
- health / capability checks.

Initial adapters:
- a local embedding model adapter for self-hosted installations;
- an OpenAI-compatible remote embeddings adapter.
Provider-specific model names and API keys are configuration values. Secrets are only read server-side and never returned by health endpoints.

## Storage and migration strategy

- Implement a repository protocol; keep the old JSON repository as a legacy import/export adapter and test fixture, not as the production multi-tenant backend.
- Add Alembic migrations, PostgreSQL integration tests and a pinned pgvector extension/version.
- Import with a stable migration ID and checkpoints. Dry-run first.
- Preserve record IDs, content, status, provenance, ownership, record versions and timestamps.
- Generate embeddings after canonical rows are committed. An embedding failure marks the record pending_index; it must not cause content loss.
- Reconcile source and target by row count, per-record content hash and metadata/provenance hash.
- Keep immutable source backups until a restore drill and evaluation pass.
- Rollback is a configuration switch back to the source snapshot or previous DB snapshot; do not attempt destructive reverse migration from an incomplete target.

## Index and query performance

Start with pgvector HNSW for the selected distance metric and benchmark real workload sizes. Validate:
- filtered ANN recall, not only unfiltered nearest-neighbour performance;
- tenant/collection skew;
- small and large tenants;
- concurrent ingestion and recall;
- index build, vacuum and reindex behaviour;
- P50/P95/P99 latency and memory/CPU use.
Consider iterative scans and partial indexes where they improve filtered recall and latency. Do not select IVFFlat or HNSW solely by convention; keep the decision evidence-based.

## Operational requirements

- TLS for network connections; secrets injected at runtime.
- Database migrations run as a controlled deployment step.
- Pool limits, statement timeouts, lock timeouts and bounded embedding concurrency.
- Backup + point-in-time recovery strategy, restore tests and retention policy.
- Metrics for embedding queue depth, failure/retry rate, index coverage, stale embeddings, recall latency, filtered candidate counts and provider cost.
- Structured traces omit raw text by default; content-level traces are opt-in and retention-controlled.

## Alternatives considered

- Keep JSON files and add an in-memory vector index: rejected for a commercial multi-user product because persistence, concurrent writes and tenant isolation would remain fragmented.
- SQLite + sqlite-vec as the primary production store: deferred; useful for a future local embedded profile, but two production storage modes would increase support and migration complexity in v1.
- Dedicated vector database (Qdrant / Weaviate): deferred; it adds a second data system and synchronization problem while PostgreSQL already needs to own metadata, lifecycle, access policy and audit records.
- Store vectors only: rejected because embeddings cannot explain, reproduce or safely correct the source memory.

## Consequences

Positive:
- One transactional system of record for records, policy metadata, events and vectors.
- SQL authorization and RLS can enforce access before ANN ranking.
- Clear path to hybrid retrieval and measurable filtered recall.
- Existing JSON content can be migrated without throwing away the current safety work.

Costs:
- PostgreSQL and pgvector become required production dependencies.
- Security-domain partition management becomes an operational responsibility; partition creation, migration and cleanup require audited lifecycle jobs.
- A shared global ANN index is not an accepted multitenant isolation design.
- Embedding model and dimension changes require explicit re-indexing.
- DB operations, schema migrations and backup drills become part of product ownership.
- Chinese and multilingual lexical retrieval requires deliberate tokenizer design.
