# REST API

Base URL: `http://127.0.0.1:8000` (default local bind). Interactive docs at `/docs`,
schema at `/openapi.json`.

## Conventions

- **Session header:** `X-NanoMemory-Session-ID: <uuid4>`.
  - single-session mode (default): omitted header means the fixed default session.
  - multi-session mode: the header is required (`400 session_id_required`); malformed ids are
    `400 session_id_invalid`, unknown ids `404 session_not_found`. There is no implicit
    fallback to the default session.
- **Admin auth:** `Authorization: Bearer <NANOMEMORY_ADMIN_TOKEN>` for destructive endpoints.
  When the token is unset, those endpoints answer `503 admin_operations_disabled`.
- **Request id:** sent back in `X-NanoMemory-Request-ID` and included in every error body.
- **Errors:**

```json
{"code": "version_conflict", "message": "working_memory version conflict: expected 1, current 2.",
 "details": {"expected_version": 1, "current_version": 2}, "request_id": "…"}
```

- Write requests require a valid `Content-Length` (411 otherwise) and are limited to
  `NANOMEMORY_REQUEST_MAX_BYTES` (413).
- Rate limit: 60 requests/minute per client by default (429 with `retry_after_seconds`).
  Session creation and agent runs have stricter buckets.

## Endpoints

### Health

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | status, provider mode, degraded reasons, storage/lock/disk checks, budgets, schema versions |

`/health` stays reachable in maintenance mode (and reports `maintenance_mode: true`).

### Sessions

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/sessions` | multi-session mode only; server-generated UUID4; no client-supplied id; rate limited |

### Working Memory

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/memory/working` | document, markdown, `memory_version`, hash |
| PUT | `/api/v1/memory/working` | requires `expected_version`; validates, then commits in one lock |
| POST | `/api/v1/memory/compress` | preview (`apply=false`) or commit a compression |

PUT body:

```json
{"expected_version": 3, "current_goal": "…", "status": "in_progress",
 "in_progress": ["…"], "blockers": [], "next_action": "…",
 "relevant_context": [], "completed": [], "allow_task_removal": false}
```

`409 version_conflict` on a stale version. A proposal that silently drops unfinished
`in_progress` items is rejected (`422`) unless `allow_task_removal=true`.

### Long-term Memory

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/memory/long-term` | filters: `category`, `tag`, `status`, `include_archived`, `limit`, `offset` |
| POST | `/api/v1/memory/long-term` | create; secrets are rejected |
| GET | `/api/v1/memory/long-term/{id}` | one record |
| PATCH | `/api/v1/memory/long-term/{id}` | requires `if_version`; `409` on mismatch |
| DELETE | `/api/v1/memory/long-term/{id}` | soft archive by default; `permanent=true` needs admin |
| GET | `/api/v1/memory/capacity` | `capacity_used`, `capacity_limit`, `capacity_percent` |
| GET | `/api/v1/memory/statistics` | counters for the dashboard (no memory contents) |
| GET | `/api/v1/memory/export` | admin only; full export |

### Episodes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/memory/episodes` | `session_id`, `event_type`, `since`, `until`, `limit`, `include_corrected` |
| POST | `/api/v1/memory/episodes` | append; supports `correction_of` |
| GET | `/api/v1/memory/episodes/quarantine` | pending quarantine records |
| POST | `/api/v1/memory/episodes/quarantine/acknowledge` | admin + `confirm: true` + reason; permits appends, not a repair |

### Agent

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/agent/run` | `task`, optional `max_iterations`, `timeout_seconds`, `allow_write_memory` |

The response reports `status`, `iterations`, `tool_calls`, `memory_update`
(`applied`, `memory_version`, `error`), retrieval/context accounting, budget consumption and
per-call usage (`usage_source=exact` only when the provider reported usage).

Statuses: `completed`, `in_progress`, `error`, `budget_exceeded`, `timeout`,
`iteration_limit`, `cancelled`.

### Metrics

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/metrics/usage` | newest first; filters `since`, `until`, `model`, `provider`, `limit` |
| GET | `/api/v1/metrics/summary` | totals, exact vs estimated split, latency p50/p95, cost by currency |

### Admin

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/memory/backup` | create a manifest-backed backup |
| GET | `/api/v1/memory/backups` | list manifests |
| POST | `/api/v1/memory/backups/{id}/verify` | recompute every file hash |
| POST | `/api/v1/memory/restore` | `{backup_id, confirmation: <backup_id>, force}` |
| POST | `/api/v1/maintenance/migrate` | run pending schema migrations |

Restore also needs `force=true` when a session version would move backwards. A version
regression without `force` returns `400 confirmation_required`.

## Status codes used

| Code | Meaning | Typical error codes |
| --- | --- | --- |
| 200/201 | success | – |
| 400 | malformed request / missing confirmation | `invalid_request`, `confirmation_required`, `session_id_required` |
| 401 | admin auth missing/invalid | `admin_auth_required`, `admin_auth_invalid` |
| 403 | permission denied on the filesystem | `permission_denied` |
| 404 | unknown route/session/record/backup | `not_found`, `session_not_found`, `memory_not_found`, `backup_not_found` |
| 409 | version conflict or quarantined log | `version_conflict`, `quarantine_unacknowledged` |
| 411/413 | size problems | `payload_too_large`, `file_too_large`, `request_budget_exceeded` |
| 422 | validation/secret/capacity | `validation_failed`, `secret_detected`, `memory_capacity_reached`, `schema_unsupported` |
| 429 | rate limit / budget | `rate_limit_exceeded`, `budget_exceeded` |
| 500 | transaction failure | `txn_commit_failed`, `corrupt_data`, `restore_failed` |
| 502/504 | provider failures | `provider_auth`, `provider_rate_limit`, `provider_timeout`, `provider_invalid_response` |
| 503 | maintenance/lock/session state/unconfigured provider | `maintenance_mode`, `lock_timeout`, `session_recovery_required`, `admin_operations_disabled`, `provider_not_configured` |

## Examples

```bash
# working memory
curl -s http://127.0.0.1:8000/api/v1/memory/working

curl -s -X PUT http://127.0.0.1:8000/api/v1/memory/working \
  -H "Content-Type: application/json" \
  -d '{"expected_version":1,"current_goal":"Ship v1","status":"in_progress","next_action":"write docs"}'

# long-term memory
curl -s -X POST http://127.0.0.1:8000/api/v1/memory/long-term \
  -H "Content-Type: application/json" \
  -d '{"content":"The retriever honours a hard token budget.","category":"architecture","tags":["retrieval"]}'

# agent run
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"task":"summarise the current goal"}'

# admin backup
curl -s -X POST http://127.0.0.1:8000/api/v1/memory/backup \
  -H "Authorization: Bearer $NANOMEMORY_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"label":"manual"}'
```

## Cognitive memory endpoints (0.2.0)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/memory/remember` | run the formation pipeline over free text |
| GET | `/api/v1/memory/recall?q=&limit=&user_id=&memory_type=` | hybrid retrieval with per-signal scores |
| GET | `/api/v1/memory/{id}/explain` | provenance, supersede chain and audit trail |
| POST | `/api/v1/memory/{id}/confirm` | promote `pending_confirmation` to `active` |
| POST | `/api/v1/memory/{id}/reject` | archive a pending memory |
| POST | `/api/v1/memory/lifecycle/run?apply=false` | evaluate retention (dry-run by default) |

`recall` returns `intent`, `insufficient_context`, `fallback_reason` and one entry per selected
memory with `score`, `reason` and `signals` (`keyword`, `semantic`, `relation`, `recency`,
`importance`).

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/memory/remember \
  -H "Content-Type: application/json" \
  -d '{"content":"I prefer Python for tooling.","user_id":"user_001"}'

curl -s "http://127.0.0.1:8000/api/v1/memory/recall?q=what%20language%20do%20I%20prefer&user_id=user_001"
```
