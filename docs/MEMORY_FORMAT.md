# Memory formats

Every persisted file is UTF-8, LF-terminated, and carries a `schema_version`. Loading a newer
schema version is refused (`schema_unsupported`) rather than rewritten.

## 1. Working Memory

### 1.1 `storage/working/<session_id>/memory.md`

Human readable content only — **no metadata is parsed out of this file**. Sections are fixed:

```markdown
# Working Memory

## Current Goal

Ship the NanoMemory MVP

## Status

in_progress

## Relevant Context

- target environment is Windows 11 with Python 3.12

## Completed

- memory controller skeleton

## In Progress

- wire the retriever budget

## Blockers

- waiting on review

## Next Action

run the integration tests
```

Empty lists are rendered as `- 尚無`. A missing section makes the file unparseable, which is
reported as `corrupt_data`; the store never substitutes defaults.

### 1.2 `memory.meta.json`

```json
{
  "schema_version": 1,
  "memory_version": 3,
  "session_id": "123e4567-e89b-42d3-a456-426614174000",
  "updated_at": "2026-11-10T04:00:00.123456+00:00",
  "content_sha256": "64 hex characters of the exact memory.md bytes",
  "status": "active"
}
```

Rules: `memory_version` starts at 1 and increases by one per committed transaction;
`expected_version` compares against this value; a body/meta hash mismatch moves the session to
`recovery_required` instead of being repaired silently.

### 1.3 `.state.json`

```json
{"schema_version": 1, "session_id": "...", "state": "recovery_required",
 "reason": "rollback_failed:replace_failed:13", "txn_id": "…", "updated_at": "…"}
```

`state` is one of `active`, `recovery_required`, `recovery_pending`, `maintenance`.

### 1.4 `.txn/<txn_id>/journal.json`

```json
{
  "journal_schema_version": 1,
  "txn_id": "32 hex characters",
  "session_id": "…",
  "old_exists": true,
  "old_md_sha256": "…",
  "old_meta_sha256": "…",
  "new_md_sha256": "…",
  "new_meta_sha256": "…",
  "new_md_tmp": "new_memory.md",
  "new_meta_tmp": "new_memory.meta.json",
  "old_md_snapshot": "old_memory.md",
  "old_meta_snapshot": "old_memory.meta.json",
  "status": "prepared",
  "started_at": "2026-11-10T04:00:00+00:00",
  "completed_at": null
}
```

`status` ∈ `prepared | committed | rolled_back`. Paths are relative names inside the
transaction directory, never absolute. A first-creation transaction has `old_exists=false`
and `null` old hashes (never fake empty snapshots).

## 2. Long-term Memory (`storage/long_term/knowledge.json`)

```json
{
  "schema_version": 1,
  "updated_at": "2026-11-10T04:00:00+00:00",
  "records": [
    {
      "id": "mem-0123456789abcdef",
      "content": "The deployment target is Windows 11.",
      "category": "project",
      "created_at": "2026-11-10T03:00:00+00:00",
      "updated_at": "2026-11-10T03:00:00+00:00",
      "source": "user",
      "source_type": "user",
      "confidence": "high",
      "importance": 4,
      "tags": ["deployment", "windows"],
      "status": "active",
      "trust_level": "user_provided",
      "version": 1,
      "schema_version": 1,
      "related_ids": [],
      "review_reason": null
    }
  ]
}
```

Field semantics:

| Field | Values | Rule |
| --- | --- | --- |
| `confidence` | `low`, `medium`, `high` | model-proposed values are clamped down (model output can never be `high`) |
| `importance` | 1–5 | model-proposed values are capped at 3 |
| `trust_level` | `untrusted`, `user_provided`, `verified`, `unknown` | clamped by `source_type`; a model cannot self-declare `verified` |
| `status` | `active`, `archived`, `conflict`, `needs_review` | delete is an archive by default |
| `version` | ≥1 | every update requires `if_version`; mismatch → 409 |
| `related_ids`, `review_reason` | – | set when a duplicate/related record is detected |

Conflict policy: identical content under another id → `conflict`; same category with tag
overlap ≥ threshold → `needs_review` ("possibly related"). Similar tags are never treated as
proof of a semantic contradiction.

Capacity: 5000 active records by default. Exceeding it returns `memory_capacity_reached`; no
record is dropped, overwritten or sharded automatically.

## 3. Episodic Memory (`storage/episodic/events.jsonl`)

One JSON object per line, append-only:

```json
{
  "event_id": "evt-0123456789abcdef0123",
  "timestamp": "2026-11-10T04:00:00+00:00",
  "session_id": "…",
  "event_type": "memory_updated",
  "summary": "working memory committed",
  "related_memory_ids": ["mem-…"],
  "source": "nanomemory",
  "outcome": "ok",
  "schema_version": 1,
  "correction_of": null,
  "trust_level": "verified",
  "payload": {"version": 2}
}
```

- Unknown `event_type` values are accepted if they look like a lowercase slug (forward
  compatible), but the documented ones are used by the engine.
- Corrupt lines are **never removed**. They are copied to `events.corrupt.jsonl`:

```json
{"schema_version":1,"quarantine_id":"qr-…","source_file":"events.jsonl","line_no":17,
 "raw_line":"…","raw_encoding":"utf-8","reason":"line is not a valid episode record",
 "detected_at":"…","content_hash":"…","session_id":null,"event_id":null}
```

- While unacknowledged quarantine records exist, appends are refused
  (`quarantine_unacknowledged`). An admin can acknowledge specific ids with a reason; that
  permits appends but does not claim the line was repaired.
- Corrections: a new event with `correction_of=<event_id>`. The default read view hides the
  corrected original; `include_corrected=true` returns the raw stream. A correction whose
  target is quarantined or missing is `unresolved` and excluded from the latest view.

## 4. Metrics (`storage/metrics/usage.jsonl`)

```json
{"schema_version":1,"request_id":"req-…","timestamp":"…","provider":"mock","model":"mock-echo",
 "usage_source":"mock","prompt_tokens":null,"completion_tokens":null,"total_tokens":null,
 "estimated_tokens":312,"latency_ms":4.2,"cost":null,"cost_currency":null,
 "session_id":"…","kind":"agent","benchmark":null,"estimator":"heuristic-cjk1-latin4-v1"}
```

`usage_source` is one of `exact` (provider reported usage), `estimated` (local heuristic),
`unknown`, `mock`. Cost is `null` unless a price table is configured for the model.

Rotation: at `NANOMEMORY_METRICS_MAX_BYTES` the current file becomes `usage.1.jsonl`, older
ones shift up to `NANOMEMORY_METRICS_BACKUP_COUNT`, all inside the metrics lock. Corrupt lines
are copied to `usage.corrupt.jsonl` and counted; they never block memory operations.

## 5. Backups (`storage/backups/<backup_id>/`)

```
<backup_id>/
├── manifest.json
├── long_term/knowledge.json
├── episodic/events.jsonl
├── metrics/usage.jsonl
└── working/<session_id>/{memory.md,memory.meta.json,.session.json,.state.json}
```

```json
{"schema_version":1,"backup_id":"uuid4","created_at":"…","label":"manual",
 "product_version":"0.1.0","schema_versions":{"long_term":1,"working_meta":1},
 "files":[{"rel_path":"long_term/knowledge.json","sha256":"…","size":123,
           "memory_version":null,"schema_version":1}],
 "session_ids":["…"],"restore_txn_id":null}
```

Paths are relative to the storage root; absolute paths and `..` are rejected at restore time.
Transaction directories (`.txn/`) are deliberately **not** backed up: a backup captures
consistent official state, not in-flight material.

## 6. Schema versions and migration

| Format | Version | Migration |
| --- | --- | --- |
| working markdown | 1 (implicit) | none; body is free-form within fixed sections |
| `memory.meta.json` | 1 | future versions rejected |
| transaction journal | 1 | future versions rejected |
| `knowledge.json` | 1 | `0 → 1` normalises legacy records (adds provenance defaults) |
| episodes | 1 | per-line; newer versions are quarantined |
| quarantine records | 1 | – |
| backup manifest | 1 | newer manifests refused |
| usage records | 1 | newer records counted as corrupt |

Migration protocol (`core/migrations.py`): full lock set → durable maintenance marker →
durable backup → migration journal (`started`) → transform → verify → atomic replace →
journal `completed` → clear marker. On failure the backup is restored and verified; if that
fails the marker stays and the service remains in maintenance mode.
