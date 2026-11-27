# Scope brief — NanoMemory v2 (vector memory)

Status: approved for implementation · Owner: product + engineering · Version: 2.0

## 1. Problem

NanoMemory v1 stores long-term memory as text in `knowledge.json` and ranks it with BM25
plus character-trigram similarity. That is cheap and dependency-free, but it has three product
limits:

1. **Paraphrase recall is poor.** "我的蓝牙耳塞坏了" does not find "耳机保修 12 个月" because
   the two strings share almost no characters. Measured on the v2 eval set, the lexical-only
   path recalls 0 % of the semantic subset.
2. **There is no tenant boundary.** A single JSON file with an optional `user_id` filter is an
   application-level filter, not an access-control boundary. One code path that forgets the
   filter leaks another customer's memory.
3. **There is no evidence trail.** Answers cannot be reconstructed after the fact: which
   candidate set was scanned, which chunk won, with what score, at what cost.

## 2. Target user

Teams embedding memory into a support/agent product that must answer from customer history and
must be able to prove *why* it answered — and must never cross tenant lines.

## 3. In scope (v2.0)

| Area | Commitment |
| --- | --- |
| Embedding abstraction | provider interface with an offline hashing implementation and an OpenAI-compatible implementation; dimension is validated at write time |
| Vector store | SQLite-backed store with transactional version history and **SQL-level pre-filtering** on tenant, user, ACL, state, category, type, tags and expiry |
| Hybrid retrieval | dense cosine + BM25 fused with intent weights and recency/importance, per-signal explainability, explicit refusal when evidence is below a calibrated floor |
| Lifecycle | `pending_embedding → active → archived → deleted → rolled_back` with guarded transitions, rollback to any historical version, reactivation |
| Embedding failure | two-phase write: the record is accepted as `pending_embedding`, the API returns a typed 503, and a retry endpoint finishes the job |
| Tenant scope | scope travels only in an HMAC-signed header envelope from the gateway; request bodies reject scope fields |
| Observability | one trace row per call with ids, scores, filter, latency, tokens, cost, refusal flag; trace lookup and list endpoints |
| Feedback loop | 👍/👎 linked to `trace_id`; thumbs-down export produces deduplicated eval candidates |
| Evaluation | 44-case bilingual dataset (29.5 % adversarial), automated Recall@K / MRR / refusal / stale checks, calibration tool, CI quality gate |
| Migration | script that re-embeds an existing `knowledge.json` into the vector store without touching the source file |

## 4. Non-goals (explicitly out of scope for v2.0)

- **No multi-node vector database.** pgvector/Qdrant adapters are a v2.1 decision; the store
  interface is kept narrow so the swap does not touch the service or API.
- **No semantic reranker model.** Cross-encoder reranking is deferred; the eval set will show
  whether hybrid fusion is enough before adding latency.
- **No chunking of long documents.** One memory = one vector. Documents over 20 k characters are
  rejected rather than silently truncated.
- **No automatic memory extraction in this layer.** Formation stays in v1's `MemoryFormation`;
  v2 stores what it is given.
- **No answer generation.** NanoMemory returns evidence and a refusal decision; grounding the
  final sentence is the gateway's job (mem-guard).
- **No cross-region replication, no sharding, no streaming ingestion.**
- **No UI work.** The dashboard is unchanged in v2.0; the API is the product surface.

## 5. Acceptance criteria (Given/When/Then)

### Story A — write and embed a memory

1. **Given** a signed context for tenant T and user U,
   **when** a client posts content with a unique `Idempotency-Key` twice,
   **then** exactly one memory exists, the second response is a 200 replay with the same id, and
   no second embedding call is made.
2. **Given** the embedding provider is down,
   **when** the client posts a memory,
   **then** the API returns 503 `embedding_failed` with the pending memory id, the record is
   stored as `pending_embedding`, and it is invisible to search until retried.
3. **Given** a request body that contains `tenant_id` or `user_id`,
   **when** it is sent to the vector endpoints,
   **then** the API returns 422 and no record is written.

### Story B — scoped hybrid retrieval

1. **Given** two tenants with identical content,
   **when** tenant A searches,
   **then** only tenant A's memory ids are returned and `candidate_count` never counts tenant
   B's rows.
2. **Given** a query whose best content signal is below the configured floor,
   **when** the client searches,
   **then** the response has `insufficient_context=true`, an empty `items` array, and a
   `fallback_reason` naming the floor.
3. **Given** the embedding provider fails during a search,
   **when** the corpus contains a lexically matching memory,
   **then** the response is `degraded=true`, still returns the match, and the trace records the
   degradation instead of failing the request.

### Story C — update and rollback

1. **Given** a memory at version 1 and an `expected_version` of 1,
   **when** the client patches the content,
   **then** version 2 is stored, version 1 remains readable in `/versions`, and the vector is
   rebuilt.
2. **Given** the same patch with `expected_version` 99,
   **when** it is sent,
   **then** the API returns 409 `version_conflict` and the stored record is unchanged.
3. **Given** a rollback to version 1,
   **when** it succeeds,
   **then** the record is `rolled_back`, excluded from retrieval, and only an explicit
   reactivation makes it answerable again.

## 6. Success metrics

| Metric | Baseline (v1 lexical) | v2 target | v2 measured (hashing, offline) |
| --- | --- | --- | --- |
| Recall@5, support eval | see evals/reports | ≥ 0.85 | **0.871** |
| Refusal accuracy (100 % adversarial subset) | not measured | ≥ 0.75 | **1.000** |
| Stale-memory violations | not measured | 0 | **0** |
| Search p95 latency (local) | ~3 ms | < 20 ms | **1.9 ms** |
| Cross-tenant leak (test suite) | n/a | 0 | **0** |

The offline numbers use the deterministic hashing provider. The semantic subset is expected to
stay at 0 % until a real embedding endpoint is configured; `evals/calibrate.py` re-derives the
refusal floor for whichever provider is deployed.

## 7. Compatibility and rollback

- v1 endpoints (`/api/v1/memory/long-term`, `/recall`) keep working unchanged. v2 is additive.
- The vector store is a separate SQLite file under `storage/vector/vectors.db`.
- Rolling back v2 means pointing clients at the v1 endpoints; no data migration is required, and
  the v1 files were never modified by the v2 write path.
- `scripts/migrate_long_term_to_vector.py` copies (never moves) v1 records and is idempotent
  through the stable v1 record id.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Embedding provider outage blocks writes | two-phase write + retry endpoint + typed 503 |
| Provider/model change orphans stored vectors | vectors are stamped with model and dim; search reports `embedding_model_changed` instead of silently returning nothing; re-embed script |
| Cheap hashing provider mistaken for semantic search | provider name and model are in every response and trace; docs and eval break down lexical vs semantic |
| Refusal threshold tuned on one provider | floor is a setting, calibration tool and eval gate make the trade-off explicit |
| SQLite write contention | WAL, single writer lock, one process in the default deployment; pgvector is the documented scale-out path |
