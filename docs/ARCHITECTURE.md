# Architecture

This document records the decisions that the code enforces: module boundaries, the lock
matrix, the Working Memory transaction state machine, crash recovery, and the limits of the
current version.

## 1. Module map

| Module | Responsibility | Must not |
| --- | --- | --- |
| `core/config.py` | `NANOMEMORY_*` settings, derived paths, budget math | contain storage logic |
| `core/errors.py` | stable error codes + HTTP mapping | leak secrets in messages |
| `core/logging_config.py`, `redaction.py` | structured logs, redaction of every handler | print raw credentials |
| `core/paths.py` | containment, symlink/junction rejection, safe components | follow links |
| `core/fsio.py` | temp naming, flush/fsync/validate/replace, orphan temp scan | rewrite user files implicitly |
| `core/locks.py` | ordered, timed, instrumented cross-process locks | acquire in ad-hoc order |
| `core/sessions.py` | session ids, registry, per-session directories | trust client-supplied ids |
| `core/working_memory.py` | sidecar + transaction journal + in-lock rollback | merge conflicting versions silently |
| `core/recovery.py` | startup ordering (migration then sessions) | open traffic before recovery |
| `core/long_term_memory.py` | `knowledge.json`, per-record version, capacity, conflict flags | silently drop or overwrite |
| `core/episodic_memory.py` | append-only JSONL, quarantine, corrections | rewrite the original log |
| `core/backup.py` | manifest + SHA-256, pre-restore snapshot, verified restore | restore unverified bytes |
| `core/migrations.py` | maintenance marker, migration journal, schema upgrade | mutate data without the full lock set |
| `core/memory_validator.py` | schema/length/secret/provenance validation | trust model self-assessment |
| `core/secret_scanner.py` | explicit credential patterns + entropy candidates | treat the detector as the only boundary |
| `core/memory_retriever.py` | explainable scoring under a hard budget | auto-load irrelevant memory |
| `core/context_builder.py` | system/untrusted separation, escaping, request budget | put memory into the system message |
| `core/memory_compressor.py` | deterministic + opt-in LLM compression with fidelity diff | apply a lossy compression that drops critical fields |
| `core/tools.py` | `ToolSpec` metadata, `RegisteredTool`, allowlist execution | import or execute model-supplied code |
| `core/budget.py` | token/tool/time/cost budgets | claim a cost limit without prices |
| `core/agent_loop.py` | bounded loop, structured output, proposal commit | treat model output as verified memory |
| `core/metrics.py` | `usage.jsonl`, rotation under one lock | mix estimated and exact usage |
| `core/health.py` | storage/lock/disk/provider checks | expose paths or tokens |
| `core/memory_manager.py` | controller façade; the only public write path | let callers bypass validation |
| `api/` | HTTP surface, error envelope, rate limit, admin auth | read storage directly |
| `dashboard/` | Streamlit UI over HTTP | write storage directly |
| `benchmarks/` | dataset, strategies, artifacts | report unmeasured numbers |

## 2. Request flow

```
HTTP request
  -> request id middleware (log + response header)
  -> size limit + maintenance check
  -> route dependency: session resolution / admin token / rate limit
  -> MemoryManager.<operation>
       -> maintenance guard
       -> LockManager.acquire([... canonical order ...])
       -> validator (schema, length, secrets, provenance)
       -> store (atomic IO / transaction)
       -> audit episode + metrics
  -> Pydantic response or unified error envelope
```

## 3. Lock matrix

Acquisition order is fixed and enforced by sorting on `(class_index, resource)`; release is
always the reverse order. Every acquisition has a timeout (default 5 s,
`NANOMEMORY_LOCK_TIMEOUT_SECONDS`) and raises `lock_timeout` (HTTP 503). There is **no**
unlocked fallback.

| # | Resource | Lock file | Held by |
| --- | --- | --- | --- |
| 0 | maintenance | `.locks/maintenance.lock` | migration / maintenance scope |
| 1 | backup | `.locks/backup.lock` | backup, restore |
| 2 | working (global) + `working:<session>` | `.locks/working-<session>.lock` | every Working Memory read/update/recovery |
| 3 | long_term | `.locks/long_term.lock` | knowledge.json read/write |
| 4 | episodic | `.locks/episodic.lock` | JSONL append/read/quarantine |
| 5 | metrics | `.locks/metrics.lock` | usage append + rotation |

| Operation | Locks (in order) | Notes |
| --- | --- | --- |
| Working Memory read | maintenance?, working, working:\<session\> | maintenance is checked before acquisition |
| Working Memory update | working, working:\<session\> | validate + apply + snapshot + journal + audit all inside |
| Long-term read/write | long_term | capacity and conflict checks inside |
| Episode append | episodic | refuses while unacknowledged quarantine exists |
| Metrics append/rotate | metrics | single critical section for check + rotate + append |
| Backup | backup → working → long_term → episodic → metrics (short) | releases resource locks before writing the manifest |
| Restore | backup → working → long_term → episodic → metrics | pre-restore snapshot via an internal helper (never `create_backup`) |
| Migration | maintenance → backup → working → long_term → episodic → metrics | marker written only after every lock is held |

The **global `working` lock** is taken by every Working Memory operation in addition to the
per-session lock. That is what lets migration/restore block all session traffic with a single
lock while normal operations still serialise per session.

## 4. Working Memory transaction state machine

Per session layout:

```
storage/working/<session_id>/
├── memory.md                 # human readable content only
├── memory.meta.json          # schema_version, memory_version, session_id, updated_at, content_sha256, status
├── .session.json             # registry metadata
├── .state.json               # active | recovery_required | recovery_pending
└── .txn/<txn_id>/            # 32-hex uuid4
    ├── journal.json
    ├── new_memory.md
    ├── new_memory.meta.json
    ├── old_memory.md
    ├── old_memory.meta.json
    └── journal.prepared.tmp.<pid>.<32-hex>   (only during the atomic journal write)
```

Commit (all inside the working lock):

1. read `memory.meta.json`, compare `expected_version` (mismatch → 409),
2. create `.txn/<txn_id>/`,
3. durable old snapshots (temp → flush → fsync → hash verify → replace → dir fsync),
4. durable new files + verification (hash, session id, version, content hash),
5. atomic prepared journal (temp → flush → fsync → parse/validate → replace → dir fsync),
6. replace `memory.md`, then `memory.meta.json`,
7. fsync the session directory,
8. atomic journal update to `committed`,
9. audit event + retention cleanup.

Failure handling:

| Failure point | Result |
| --- | --- |
| steps 3–4 | official files untouched, transaction directory removed, audit `transaction_prepare_failed`, HTTP 500 |
| step 5 | same as above (a journal temp may exist; it is cleaned by recovery) |
| step 6, first or second replace | in-lock rollback after verifying each official file hash is old/new/absent |
| rollback verification fails / hash unknown | session persisted as `recovery_required`, transaction material kept, HTTP 500 with `details.session_state=recovery_required` |
| step 8 (`committed` journal write) | rollback to the old state; the transaction is never reported as success |

`fsync` of a directory is only possible on POSIX. On Windows the helper returns `False` and
the limitation is recorded (see SECURITY.md); the journal still makes the recovery decision
deterministic.

## 5. Startup order

```
process start
  -> read maintenance marker (no session recovery yet)
  -> marker present?
       yes: acquire maintenance + all resource locks
            migration recovery (rollback from the durable backup, or clear if nothing started)
            success -> clear marker, release locks
            failure | lock timeout -> keep marker, stay maintenance_mode, degraded
       no : continue
  -> for each session: recovery under working + working:<session>
  -> only then is read/write traffic accepted
```

Any memory/agent API call during maintenance answers `503 maintenance_mode`.

## 6. Recovery decision table (per session)

| Journal state | Official files | Action |
| --- | --- | --- |
| `committed` (newest txn) | match new hashes | keep (retention prunes later); no state change |
| `committed` | mismatch | `recovery_required`, audit `recovery_external_modification_detected` |
| `committed` (historical) | any | retained snapshot; later transactions legitimately changed the files, nothing to verify |
| `prepared` | old/new/absent combination | roll back to old snapshots (or remove first-creation files), journal → `rolled_back`, cleanup |
| `prepared` | unknown hash | fail closed: `recovery_required`, keep everything |
| `rolled_back` | old hashes or absent | cleanup once (idempotent, audit once) |
| `rolled_back` | any new hash | fail closed: `rolled_back_state_inconsistent` |
| `rolled_back` | unknown hash | fail closed: external modification |
| corrupt journal | any | quarantine journal, `recovery_required`, official files untouched |
| no journal, ≥1 mandated txn file, old enough, official hash ≠ new hash | – | delete directory, audit `orphan_txn_cleaned` |
| no journal, official hash = new hash | – | fail closed: `orphan_txn_with_partial_commit_detected` |
| no journal, no mandated txn file | – | leave untouched (diagnostic only) |
| journal temp, no journal, old enough | – | delete temp only, audit `journal_temp_cleaned` |

Session state transitions: `active → recovery_required` (transaction/rollback failure) and
`recovery_required → active` (successful startup recovery). While `recovery_required`, every
request is answered `503 session_recovery_required` and no retry of the rollback is allowed
outside recovery.

## 7. Data formats

See [MEMORY_FORMAT.md](MEMORY_FORMAT.md). Every persisted format carries a
`schema_version`; loading a newer version is refused instead of being "upgraded" silently.
Migrations run only under the maintenance lock set and keep a durable backup plus a journal.

## 8. Decisions and trade-offs

1. **Flat layout, single package.** `pyproject.toml`, `pytest` and both entry points agree on
   `nanomemory/`. No `src/` layout.
2. **Markdown body + JSON sidecar.** Human readability without parsing versions out of text.
3. **Per-transaction subdirectories.** A single fixed `.txn/journal.json` cannot both retain
   the last N snapshots and serve rollback of older transactions; one directory per
   transaction makes retention and recovery unambiguous.
4. **Global + per-session working locks.** Simpler and safer than a pure per-session matrix,
   at the cost of serialising concurrent writers in v1 (acceptable for a single-user MVP).
5. **Coarse single-file long-term store.** Keeps the format inspectable and the dependency
   surface tiny; documented 5000-record cap and a future SQLite path.
6. **Deterministic mock provider.** Makes demos and crash tests reproducible and never
   pretends to be a real model.
7. **Tool registry in code.** The model selects a name; it cannot construct a tool.
8. **Dashboard over HTTP only.** One validation/locking path for every client.

## 9. Known limitations

- Windows: no directory `fsync`; MAX_PATH (~260 characters) applies to storage roots that are
  already deep — keep the project path short or enable long-path support.
- Single worker only; the in-memory rate limiter is per-process and the health endpoint
  reports that fact rather than claiming a global limit.
- Keyword retrieval only; recall degrades on large stores (measured in the benchmark).
- Inline snapshots retain the last 10 completed transactions per session by default.
- Sessions isolate files, not users; there is no authentication of *who* may use a session.
