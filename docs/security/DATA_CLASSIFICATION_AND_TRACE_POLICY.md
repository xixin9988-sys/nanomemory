# Data classification and trace policy

Status: implemented (trace content minimisation + retention pruning), reviewed 2026-11-27.

## 1. Data classes

| Class | Examples | Storage | Default |
| --- | --- | --- | --- |
| **Secret** | API keys, private keys, passwords | must never be stored | rejected/redacted by the scanner before write |
| **Personal data** | phone, email, national id, bank card | redacted by the gateway before embedding; raw forms avoided in traces | not persisted in traces |
| **Customer content** | memory text, order notes, preferences | vector DB content column | stored (it is the product) |
| **Derived data** | embeddings, scores, hashes | vector DB / traces | stored |
| **Operational metadata** | tenant, user, request id, timings, cost | traces | stored |

The memory text is the product's data, not a log. The trace is a log, and logs have a different
threat model: they are copied, shipped, indexed and read by more people.

## 2. Trace content policy

Implemented in `nanomemory/vector/trace_policy.py`; configured by
`NANOMEMORY_TRACE_CONTENT_MODE`.

| Field | `minimal` (default) | `full` (opt-in) |
| --- | --- | --- |
| `query_raw` | **not stored** | stored, truncated to `trace_content_max_chars` |
| `final_prompt_context` | **not stored** | stored, truncated |
| `model_response` | **not stored** (NanoMemory does not generate answers) | stored if supplied, truncated |
| query hash / length | stored in `metadata` (`query_sha256`, `query_chars`) | also stored |
| filter shape, ids, scores, timings, cost, flags | stored | stored |

Why hashes: an operator can still prove that two traces came from the same query, or compare a
trace with a replayed request, without retaining the text.

Turning on `full` is a per-deployment decision that must be recorded with: legal basis,
retention window, access role, encryption at rest, and an owner. It is not a debugging toggle.

## 3. Retention

- `NANOMEMORY_TRACE_RETENTION_DAYS` (default 30) is the policy value.
- Enforcement is explicit, not a background surprise: `POST /api/v1/admin/traces/prune`
  (admin, tenant-scoped) deletes that tenant's traces older than the window and the feedback rows
  attached to them. Run it from cron with the operator's admin token.
- Feedback that references a pruned trace is deleted with it; export badcases **before** pruning
  if the feedback is still needed for the eval set.

## 4. Access

- Trace reads are tenant-scoped (`GET /api/v1/traces/{id}`), and a foreign trace is
  indistinguishable from a missing one (404).
- Pruning and badcase export require the admin token; when `NANOMEMORY_ADMIN_TOKEN` is unset the
  operations are disabled entirely.
- Traces never contain provider credentials, request bodies of other tenants, or SQL.

## 5. Export and deletion

- `GET /api/v1/feedback/export` writes deduplicated badcases to a JSONL file. The export
  contains the query text — review and redact before committing it to a public repository.
- Memory deletion is soft by default (`deleted` state); permanent purge is an operator action.
  Deleting a memory does **not** delete traces that referenced it; prune traces separately if the
  data subject request covers the audit log. Document this in the DPA.

## 6. Residual risks

1. `full` mode stores raw text; it must be treated as a second personal-data store.
2. Hash-based linkage is pseudonymisation, not anonymisation.
3. Logs shipped to an external SIEM inherit that system's retention and access controls.
4. A public badcase export is a publication decision, not a debugging step.
