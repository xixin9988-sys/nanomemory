# Vector memory API (v2)

Base URL: `http://127.0.0.1:8000`. OpenAPI: `/docs`.

## Scope headers

Scope is **never** a body or query parameter. Without a signing key (development) the service
trusts these headers and defaults to `local/local`:

    X-NM-Tenant: tenant-a
    X-NM-User: user-1
    X-NM-Roles: agent
    X-NM-Acl: team-a

With `NANOMEMORY_CONTEXT_SIGNING_KEY` set, every request must also carry:

    X-NM-Timestamp: 1764240000
    X-NM-Signature: <hex HMAC-SHA256 of "tenant\nuser\nroles\nacl\ntimestamp">

The gateway (mem-guard) produces this envelope; a client cannot mint one. In production the key
is mandatory — Settings refuses to start without it.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/memory/vectors` | write + embed (201, or 200 on idempotent replay) |
| POST | `/api/v1/memory/vectors/search` | hybrid search / refusal |
| GET | `/api/v1/memory/vectors` | list (state, pagination) |
| GET | `/api/v1/memory/vectors/{id}` | fetch one |
| GET | `/api/v1/memory/vectors/{id}/versions` | version history |
| PATCH | `/api/v1/memory/vectors/{id}` | update content/metadata/ACL |
| POST | `/api/v1/memory/vectors/{id}/archive` | archive |
| POST | `/api/v1/memory/vectors/{id}/reactivate` | archived/rolled_back → active |
| POST | `/api/v1/memory/vectors/{id}/rollback` | restore a historical version |
| POST | `/api/v1/memory/vectors/{id}/retry-embedding` | finish a pending write |
| DELETE | `/api/v1/memory/vectors/{id}` | soft delete |
| GET | `/api/v1/memory/vectors/stats` | tenant-scoped counts + provider info |
| POST | `/api/v1/feedback` | 👍/👎 for a trace |
| GET | `/api/v1/feedback` | list feedback |
| GET | `/api/v1/feedback/export` | admin: append badcases to the eval file |
| GET | `/api/v1/traces/{trace_id}` | full trace |
| GET | `/api/v1/traces` | recent traces |

## Write

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/memory/vectors \
  -H 'Content-Type: application/json' \
  -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1' \
  -H 'Idempotency-Key: order-a1001-note' \
  -d '{
    "content": "Order A1001 由顺丰发出，运单号 SF7788990011。",
    "category": "order",
    "tags": ["logistics"],
    "memory_type": "event",
    "importance": 4,
    "source": "crm",
    "source_type": "user",
    "provenance": {"source_kind": "user", "source_ref": "crm://orders/A1001"}
  }'
```

```json
{
  "record": {
    "id": "vec-8f2c…", "state": "active", "version": 1,
    "embedding": {"model": "hashing-384", "dim": 384},
    "content_sha256": "…"
  },
  "trace_id": "tr-…",
  "idempotent_replay": false
}
```

A 503 with `code=embedding_failed` means the record exists as `pending_embedding`:

```json
{"code": "embedding_failed",
 "details": {"memory_id": "vec-8f2c…", "state": "pending_embedding", "reason": "timeout"}}
```

Retry with `POST /api/v1/memory/vectors/vec-8f2c…/retry-embedding`.

## Search

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/memory/vectors/search \
  -H 'Content-Type: application/json' \
  -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1' \
  -d '{"query": "A1001 的运单号", "limit": 5, "categories": ["order"]}'
```

```json
{
  "query": "A1001 的运单号",
  "trace_id": "tr-…",
  "items": [{
    "id": "vec-8f2c…",
    "content": "Order A1001 由顺丰发出，运单号 SF7788990011。",
    "score": 0.83,
    "signals": {"dense": 0.61, "sparse": 1.0, "lexical_coverage": 0.8,
                 "recency": 0.99, "importance": 0.75},
    "reason": "dense 0.61, sparse 1.00, recency 0.99, importance 0.75"
  }],
  "candidate_count": 7,
  "insufficient_context": false,
  "content_signal": 0.61,
  "degraded": false,
  "embedding_model": "hashing-384",
  "timings_ms": {"embedding": 0.3, "retrieval": 0.6, "total": 0.9}
}
```

Refusal (HTTP 200, empty evidence):

```json
{
  "items": [],
  "insufficient_context": true,
  "fallback_reason": "best content signal 0.083 is below the floor 0.10; …",
  "content_signal": 0.083
}
```

Callers must branch on `insufficient_context` and say "not in memory" — never on "items is
short".

## Update, rollback, reactivate

```bash
curl -s -X PATCH …/memory/vectors/vec-8f2c -H 'Content-Type: application/json' \
  -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1' \
  -d '{"content": "Order A1001 已签收。", "expected_version": 1}'

curl -s -X POST …/memory/vectors/vec-8f2c/rollback \
  -H 'Content-Type: application/json' -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1' \
  -d '{"target_version": 1}'

curl -s -X POST …/memory/vectors/vec-8f2c/reactivate \
  -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1'
```

## Feedback

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H 'X-NM-Tenant: tenant-a' -H 'X-NM-User: user-1' \
  -d '{"trace_id": "tr-…", "verdict": "down", "reason": "wrong_memory"}'
```

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `context_required` | 401 | signed mode, envelope missing |
| `context_signature_invalid` | 401 | bad signature or stale timestamp |
| `tenant_scope_violation` | 403 | ACL tags not a subset of the caller's |
| `memory_not_found` | 404 | unknown id **or** another tenant's id |
| `version_conflict` | 409 | `expected_version` mismatch |
| `invalid_state_transition` | 409 | e.g. update after delete |
| `embedding_failed` | 503 | stored as `pending_embedding`, retryable |
| `embedding_dimension_mismatch` | 422 | provider/model dimension drift |
| `validation_failed` | 422 | schema violation, including smuggled scope |
