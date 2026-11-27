# Vector memory architecture (v2)

## 1. Why a separate path instead of rewriting the v1 store

The v1 long-term store is a single `knowledge.json` document with optimistic versions and a
human-readable audit trail. It is good at *being inspectable* and bad at *scoped similarity
search*: a JSON file cannot express `WHERE tenant_id = ? AND user_id IN (...)`, and moving the
file format would break every existing tool, benchmark and backup.

v2 therefore adds a parallel store with a narrow interface, and keeps v1 intact:

    REST / SDK        v1 endpoints                v2 endpoints
                         |                            |
                         v                    VectorMemoryService
                  LongTermMemoryStore                 |
                  (knowledge.json)          +---------+----------+
                                            |                    |
                                   EmbeddingProvider     SQLiteVectorStore
                                            |                    |
                                   hashing / OpenAI      memories + versions
                                                         ACL/tag projections
                                                         traces + feedback

Long-term recall can migrate to v2 without a flag day; the v1 store remains the system of record
for anything the v2 path does not own yet (Working Memory, episodes, agent memory formation).

## 2. Layers and dependency rule

| Layer | Module | May import | Responsibility |
| --- | --- | --- | --- |
| contract | `vector/schemas.py` | core memory models | every persisted and returned shape |
| storage | `vector/db.py`, `vector/store.py` | contract | SQL, transactions, versioning, pre-filter |
| embedding | `vector/embeddings.py` | contract, provider errors | text → vector, retries, cache |
| ranking | `vector/retrieval.py` | contract, v1 intent detector | dense + BM25 fusion, refusal signal |
| service | `vector/service.py` | all of the above | state machine, ACL, trace emission |
| transport | `api/routes/vector.py` | service | HTTP, scope resolution, status codes |

No layer imports the layer above it; the service never touches SQL and the store never calls an
embedding provider. That is what makes the degradation paths testable in isolation.

## 3. Data model

```sql
vector_memories(id, tenant_id, user_id, content, metadata, category, memory_type,
                valid_until, state, version, embedding, embedding_model, embedding_dim,
                content_sha256, created_at, updated_at, deleted_at, rolled_back_from,
                state_reason, schema_version)
memory_versions(memory_id, version, content, metadata, state, embedding, embedding_model,
                embedding_dim, content_sha256, acl_tags, created_at, reason)
memory_acl(memory_id, tag)      -- access projection, indexed on tag
memory_tags(memory_id, tag)     -- metadata projection, indexed on tag
traces(...)                     -- one row per call
feedback(...)                   -- 👍/👎 linked to trace_id
idempotency(key, tenant_id, memory_id, created_at)
```

The embedding is a little-endian `float32` blob. Vectors are L2-normalised at write time, so
cosine similarity is a dot product; the store never trusts an unnormalised vector from a caller.

## 4. Pre-filtering: the security-critical query

Every candidate query starts from this clause and only then computes similarity:

```sql
SELECT ... FROM vector_memories m
WHERE m.tenant_id = ?                                    -- mandatory, from signed context
  AND m.state IN ('active')                              -- pending/archived/deleted excluded
  AND (m.user_id = ? OR EXISTS (                          -- own rows or explicitly shared rows
        SELECT 1 FROM memory_acl a
        WHERE a.memory_id = m.id AND a.tag IN (?, ?)))
  AND m.category IN (?, ?)                               -- optional taxonomy filters
  AND EXISTS (SELECT 1 FROM memory_tags t                  -- AND semantics: all tags must match
              WHERE t.memory_id = m.id AND t.tag = ?)
  AND (m.valid_until IS NULL OR m.valid_until > ?)         -- expiry
  AND m.embedding_model = ?                                -- vector-space compatibility
LIMIT ?
```

Design rules that follow from this:

- The filter is built from `MemoryFilter`, which is constructed from `TenantContext`. Request
  bodies cannot contain scope fields (`extra="forbid"`), so there is no path from client JSON to
  the WHERE clause.
- ACL membership is a join table, not a JSON array. "Does this user see this row" is answered by
  the database, not by a Python `if`.
- `allow_expired` exists for audit views, never for retrieval.
- The service records `candidate_count` (rows the filter admitted) in every trace; an unexpected
  number is a diagnosable signal, not a mystery.

## 5. Hybrid scoring

For query `q` and candidate `m`:

    S(m, q) = w_d · dense(m, q) + w_s · sparse(m, q) + w_r · recency(m) + w_i · importance(m)

| Signal | Definition | Range | Why |
| --- | --- | --- | --- |
| `dense` | cosine(embed(q), embed(m)) | 0–1 | paraphrase, cross-lingual, semantics |
| `sparse` | BM25 over the **same** candidate set, min-max normalised | 0–1 | identifiers, SKUs, names that embeddings blur |
| `recency` | exp(-age/120 days) | 0–1 | fresh facts win ties |
| `importance` | (importance-1)/4 + confidence bonus | 0–1 | user-flagged weight |

Weights depend on the detected intent (preference questions lean dense; project-state questions
lean sparse), reusing v1's `detect_intent` so the two retrieval paths agree on intent vocabulary.

**Refusal is a separate, absolute signal.** BM25 is normalised per candidate set, so it is
relative and cannot answer "is there an answer at all". Refusal uses

    signal = max(best dense cosine, best IDF-weighted query-token coverage)

and refuses when `signal < NANOMEMORY_VECTOR_CONTENT_FLOOR` (default 0.10, calibrated by
`evals/calibrate.py`). `insufficient_context=true` implies `items=[]`; the weak candidates
remain visible in the trace for debugging.

## 6. Failure modes and degradation

| Failure | Detection | Behaviour | Trace |
| --- | --- | --- | --- |
| embedding timeout during write | retryable provider error | retries, then `pending_embedding` + 503 with `memory_id` | `event=embedding_failed` |
| embedding auth error | non-retryable | no retry, same pending path | `error=ProviderError` |
| embedding down during search | exception around `_embed` | BM25-only ranking, `degraded=true` | `degraded=true` |
| model changed, vectors orphaned | 0 dense candidates but rows exist | empty answer with `embedding_model_changed` reason | `degraded=true` |
| dimension mismatch | query vs stored matrix shape | 422 `embedding_dimension_mismatch` | — |
| SQLite busy | `busy_timeout` 5 s | 503 `vector_store_unavailable` at the service boundary | — |
| trace write failure | exception caught | request succeeds, warning logged | — |

The rule is: **degrade retrieval quality, never silently answer as if nothing happened.**

## 7. Observability

Each call writes one `TraceLog`:

```text
trace_id, kind, tenant_id, user_id, request_id,
query_raw, filter_applied, retrieved_chunk_ids, similarity_scores,
final_prompt_context, model_response, ttft_ms, total_latency_ms,
input_tokens, output_tokens, estimated_cost,
embedding_model, insufficient_context, degraded, metadata, created_at
```

Feedback rows reference `trace_id`; `GET /api/v1/feedback/export` (admin) appends
thumbs-down traces to `evals/badcases.jsonl` in eval-dataset shape, deduplicated by trace id.
That is the loop from production failure to regression case.

## 8. Performance envelope

- 24–5 000 memories per tenant, 384–1 536 dimensions.
- Brute-force dot product over the filtered set: ~1.5 ms for 24 rows, ~25 ms for 5 000 rows at
  384 dims on a laptop. Acceptable for the target workload; above that, the store interface is the
  swap point for pgvector.
- Writes serialise on a single writer lock; embedding (the slow part) happens outside the lock, so
  concurrent writes only contend on the fast SQLite commit.
- WAL mode, `synchronous=NORMAL`: durable across process crashes, may lose the last commit on
  OS power loss. Documented, not hidden.

## 9. Alternatives considered

| Option | Why not now |
| --- | --- |
| pgvector | best scale-out path, but adds a Postgres dependency to a "pip install and go" product. The `SQLiteVectorStore` interface is deliberately the same shape (`candidates` / `score_all` / `save`) so a pgvector adapter is additive. |
| Qdrant / Milvus | network service, operational burden for a single-node v2. |
| Store vectors in `knowledge.json` | no SQL pre-filter, no transactions, rewrites the whole file per write. |
| Chroma | convenient, but its filtering semantics and persistence lifecycle would leak into the product contract. |
