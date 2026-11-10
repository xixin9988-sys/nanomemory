# NanoMemory 2.0 — Architecture Review & Phased Build Spec

Status: **review + executable plan**, not yet implemented.
Baseline: commit `44db3a7` (v0.1.0, 271 tests, benchmark artifacts in `benchmarks/results/`).
Audience: the next coding agent (Codex / DSH / Claude Code) and the maintainer.

---

## 0. Executive summary

Positioning: **a universal, model-agnostic AI memory engine** — install it, connect an app,
and it forms, organises, retrieves, updates, forgets and explains memory on its own. It is not
a Markdown notebook, not a vector-DB wrapper, and not a DSH-only plugin.

The v0.1.0 build solved the hard *safety* half: crash-safe transactions, cross-process locks,
backup/restore, quarantine, secret scanning, provenance fields, admin-only destructive
operations. Those stay. What v0.1.0 did **not** solve is *memory quality*: the benchmark shows
**39 % fact recall** against full history's 100 %.

That number is not a verdict on memory engines — it is a defect in one layer. The diagnosis
below shows most misses are retrieval failures, not storage failures, and that the current
retriever is a single lexical scorer with a hard 8-item cap.

**Do not bolt more safety modules onto this.** Fix retrieval to a measurable bar, add automatic
memory formation, then make the product installable in one command.

Non-goals for 2.0: multi-tenant SaaS, vector-DB replacement claims, "90 % token savings"
marketing, model-specific behaviour.

---

## 1. Baseline and evidence

### 1.1 What exists (and is worth keeping)

| Layer | Module(s) | State |
| --- | --- | --- |
| L0 Foundation | `core/fsio.py`, `core/locks.py`, `core/errors.py`, `core/paths.py`, `core/redaction.py`, `core/logging_config.py` | solid, 100+ tests |
| L0 Recovery | `core/working_memory.py` (sidecar journal), `core/recovery.py`, `core/migrations.py` | solid |
| L1 Storage | `core/working_memory.py`, `core/long_term_memory.py`, `core/episodic_memory.py`, `core/backup.py` | files only, single-file JSON |
| L2 Model | `core/memory_models.py` | flat record, no entity/relation/validity |
| L3 Formation | `core/memory_validator.py` (rejects bad input) | **no extraction** |
| L4 Consolidation | `core/memory_compressor.py` (WM text only) | **no dedupe/merge across records** |
| L5 Retrieval | `core/memory_retriever.py` | keyword only, one scorer, cap 8 |
| L6 Controller | `core/memory_manager.py` | façade, no lifecycle policy |
| L7 Interface | `api/`, `dashboard/`, `mcp_server.py`, `dsh-plugin/` | REST + MCP exist, **no SDK, no CLI** |

### 1.2 Why recall is 0.39 — measured, not guessed

`reports/analyse_recall.py` replays the long-history benchmark against the real retriever and
checks each required fact for three states: present in the selected context, stored-but-not-
selected with shared keywords, or stored with **no shared keyword** at all.

```
required facts: 28
present in selected context: 11   (39.3 %)
absent, shared keywords but not selected (ranking/cap/budget): 9
absent, zero meaningful keyword overlap (lexical gap):         8
```

Implications:

* **8/28 (29 %) cannot be fixed by tuning scores.** The task and the fact share no word:
  "How does NanoMemory handle secrets found in memory content?" vs the fact
  "Secret scanning rejects writes by default with code secret_detected". Only semantic
  matching, entity linking or query expansion can reach these. This is the embedding /
  relation argument, quantified.
* **9/28 (32 %) are ranking and budget defects.** They share keywords but lose to noisy
  neighbours (e.g. "python" matches six facts) because the scorer is a flat sum with no term
  weighting, no query-type routing, and a hard `MAX_LONG_TERM_ITEMS = 8`.
* **Storage is not the problem.** Every required fact was stored and readable; 17/17 misses
  were retrieval misses. The "safe storage" work is validated by this test.

### 1.3 What this changes about priorities

The user-facing symptom is "it often doesn't remember". The fix order is therefore:
retrieval quality → automatic formation → install/UX → graph/temporal → production. Safety
work is *paused* except where a new capability creates a new failure mode (e.g. LLM extraction
must not be able to write unreviewed claims).

---

## 2. Target architecture (seven layers)

```
L7  Interface      SDK · REST · MCP · CLI · Dashboard
L6  Cognitive      formation · consolidation · lifecycle · retrieval routing · explanations
L5  Retrieval      BM25 + embeddings + entity/relation + RRF fusion + rerank + budget packing
L4  Consolidation  dedupe · merge · conflict resolution · summarisation
L3  Formation      extraction · classification · importance · provenance assignment
L2  Model          memory records (validity, status, provenance) · entities · relations
L1  Storage        JSON (default) → SQLite+FTS5 (scale) · backups · export/import
L0  Foundation     transactions · locks · recovery · validation · secret scanning
```

Layer ownership rule: **L6 may propose, L0 decides.** No model output reaches L1 without
deterministic validation and a provenance record.

---

## 3. Module-level decisions

### 3.1 Keep as-is (no rewrite)

`core/fsio.py`, `core/locks.py`, `core/paths.py`, `core/errors.py`, `core/redaction.py`,
`core/logging_config.py`, `core/working_memory.py`, `core/recovery.py`, `core/migrations.py`,
`core/backup.py`, `core/secret_scanner.py`, `core/context_builder.py`, `core/budget.py`,
`core/tools.py`, `core/metrics.py`, `core/health.py`, `api/`, `mcp_server.py`, `dsh-plugin/`.

These carry the guarantees the project already earned. Changing them now would re-open settled
questions and invalidate 200+ tests.

### 3.2 Refactor (same responsibility, better internals)

| Module | Change |
| --- | --- |
| `core/long_term_memory.py` | split into `storage/repository.py` (persistence) + `retrieval/index.py` (BM25 index). Keep the JSON file format as the default backend and the public methods. Add `iter_all()` and bulk operations; do not rewrite callers |
| `core/memory_retriever.py` | becomes a thin façade over `retrieval/pipeline.py`: query analysis → candidate generation → fusion → rerank → budget packing. Keep `retrieve()` signature and `RetrievalResult` shape so API/agent/tests keep working |
| `core/memory_models.py` | add v2 fields (type, validity, status, provenance, entities) with defaults; keep v1 fields and parsing. Bump `LONG_TERM_SCHEMA_VERSION` to 2 with a migration |
| `core/memory_manager.py` | add `remember()/recall()/forget()/explain()` and formation hook; existing API stays |
| `core/agent_loop.py` | after a run, feed the transcript to `memory_formation` instead of only accepting model proposals; keep the current explicit `memory_update` path |
| `core/memory_compressor.py` | rename responsibility: WM compression stays; record-level consolidation moves to `consolidation/` |

### 3.3 New packages

```
nanomemory/
├── memory/                    # v2 domain layer (pure, no IO)
│   ├── models.py              # MemoryRecord v2, Provenance, Validity, Status
│   ├── entities.py            # Entity, Relation, alias resolution
│   └── policy.py              # lifecycle/importance/forgetting rules
├── formation/
│   ├── extractor.py           # candidate extraction (rules first, LLM optional)
│   ├── classifier.py          # memory_type, scope, sensitivity
│   ├── admission.py           # deterministic gate: dedupe keys, trust, quotas
│   └── pipeline.py            # ingest(text|turn|document) -> decisions + episodes
├── consolidation/
│   ├── dedupe.py              # exact/semantic duplicate detection
│   ├── merge.py               # merge, supersede, conflict marking
│   └── summarise.py           # episodic → semantic rollups
├── retrieval/
│   ├── bm25.py                # pure-Python BM25 (no dependency), FTS5 later
│   ├── embeddings.py          # provider interface + cache + optional local/remote backends
│   ├── vector_index.py        # brute-force cosine for ≤10k, ANN later
│   ├── fusion.py              # RRF + per-query adaptive weights
│   ├── rerank.py              # cross-feature reranker (deterministic; LLM optional)
│   └── router.py              # query type -> strategy/profile
├── storage/
│   ├── repository.py          # MemoryRepository protocol (JSON impl first)
│   ├── json_repository.py     # current knowledge.json, v2 schema
│   └── sqlite_repository.py   # phase P2: records + FTS5 + optional sqlite-vec
├── lifecycle/
│   ├── states.py              # ACTIVE/SUPERSEDED/CONFLICT/ARCHIVED/PENDING_CONFIRMATION
│   └── scheduler.py           # decay, TTL, archive, review queue
├── explain/
│   └── trace.py               # why-stored / why-retrieved / what-superseded-what
├── sdk.py                     # Memory facade: remember/recall/forget/explain
└── cli.py                     # init/start/doctor/migrate/export/import
```

Everything above is additive; no existing module is deleted in 2.0.

---

## 4. Memory model v2

```python
class MemoryRecord(BaseModel):
    id: str
    content: str
    memory_type: Literal[
        "fact",
        "preference",
        "decision",
        "project_state",
        "event",
        "procedure",
        "relation",
        "profile",
    ]  # new
    scope: str  # user / project / session / global
    source: str  # human-readable origin
    source_type: SourceType  # user | model | tool | import | consolidation
    confidence: Confidence  # clamped by source rules (unchanged)
    importance: int  # 1..5, clamped (unchanged)
    created_at: str
    updated_at: str
    valid_from: str | None  # NEW: when the fact became true
    valid_until: str | None  # NEW: when it stopped being true
    status: MemoryStatus  # ACTIVE|SUPERSEDED|CONFLICT|ARCHIVED|PENDING_CONFIRMATION
    superseded_by: str | None  # NEW: id of the record that replaced it
    related_ids: list[str]
    entities: list[str]  # NEW: entity ids referenced
    tags: list[str]
    provenance: Provenance  # NEW: structured chain, see below
    version: int
    schema_version: int = 2


class Provenance(BaseModel):
    origin_event_id: str  # episodic event that triggered formation
    evidence: list[Evidence]  # quoted spans with offsets
    formed_by: Literal["rule", "llm", "consolidation", "user"]
    model: str | None
    rule: str | None  # rule id when deterministic
    derived_from: list[str]  # parent memory ids for merges
    review: Literal["auto", "needs_review", "confirmed"] = "auto"
```

Rules that must be enforced in code, not prompts:

1. `source_type=model` cannot produce `confidence=high` or `trust_level=verified` (already true in v1; keep).
2. A record with `formed_by="llm"` starts as `PENDING_CONFIRMATION` unless a deterministic
   rule also matched (e.g. explicit user statement "I prefer X").
3. `supersede()` never deletes: old record → `SUPERSEDED`, `valid_until` set, `superseded_by`
   set, and an episodic `memory_superseded` event written.
4. Every record has ≥1 provenance evidence span. Records without one are rejected by admission.
5. `status=CONFLICT` is reachable only through the conflict resolver; retrieval down-ranks it
   and the API exposes both sides.

Migration: v1 records → v2 with `memory_type="fact"`, `scope="global"`,
`provenance.formed_by="rule"`, `evidence=[{source: record.source}]`, `status=ACTIVE`.
`docs/MEMORY_FORMAT.md` gets a v2 section; `schema_version=1` files remain readable.

---

## 5. Retrieval v2 (the P0 fix)

### 5.1 Pipeline

```
query
 ├─ query analysis        (length, language, entities, intent: preference/state/why/when/what)
 ├─ candidate generation  (BM25 top 50 ∪ vector top 50 ∪ graph neighbours ∪ pinned ACTIVE facts)
 ├─ fusion                (RRF, k=60; per-intent weights)
 ├─ rerank                (feature model: term coverage, entity match, type match,
 │                          recency, importance, confidence, conflict penalty)
 ├─ budget packing        (dedupe near-identical, pack by score density, keep WM block)
 └─ explanation           (per item: score components + why selected / why omitted)
```

### 5.2 BM25 without dependencies (P0)

Pure Python inverted index built lazily and cached in memory; rebuilt on write (incremental
insert) and after load. 5 000 records × ~20 terms is trivial (<5 ms query). Parameters
`k1=1.5, b=0.75`. This alone fixes the "9 outranked" class, because term weighting and length
normalisation stop a long noisy record from beating a precise one.

### 5.3 Embeddings (P0 interface, P1 default)

`EmbeddingProvider` protocol: `embed(texts) -> list[list[float]]`, `dim`, `name`.
Backends: `openai_compatible` (reuse provider config), `local` (optional
sentence-transformers, not a hard dependency), `hashing` (deterministic bag-of-words hashing —
used by tests and as a zero-cost fallback, clearly labelled as not semantic).
Cache embeddings on disk keyed by `sha256(model + text)`; never send memory content to an
embedding API without the same key/permission model as the LLM provider.

Default without any key: BM25 + entity + rerank (no embeddings). It must already beat 0.39;
the gate is stated in §10.

### 5.4 Query-type routing (no single global weight vector)

| Intent | Example | Extra weight |
| --- | --- | --- |
| preference | "what style do I like" | `memory_type=preference`, recency, importance |
| project state | "where did we leave off" | `project_state`, `valid_until is null`, recency |
| why/decision | "why did I choose Python" | `decision`, provenance evidence, graph parents |
| timeline | "what did we discuss" | episodic events, chronological packing |
| lookup | "what is the port" | BM25 exact terms, type `fact` |

Implementation: `router.py` returns a `RetrievalProfile` (weights + caps + whether to include
episodes). It is deterministic and unit-testable; an optional LLM classifier can override but
its choice is recorded in the explanation.

---

## 6. Cognitive layer (formation → consolidation → lifecycle)

### 6.1 Automatic formation pipeline

Input: a chat turn, a document, a tool result, or an agent transcript.

1. **Segment** into candidate statements (sentences/turn boundaries; rules first).
2. **Classify** `memory_type` + `scope` + sensitivity with deterministic patterns
   ("I prefer…", "we decided…", "the project uses…", dates/versions/paths).
3. **Extract** structured candidates `{content, type, entities, valid_from}`; LLM is optional
   and only enriches, never decides admission.
4. **Admission** (deterministic):
   - secret scan (existing),
   - dedupe key: normalised content + entity + type,
   - quota per turn (default ≤ 5 records),
   - trust rules (§4),
   - evidence span required.
5. **Resolve** against existing memory: `new` / `duplicate` / `update` / `supersede` /
   `conflict` (see 6.2).
6. **Persist** record + provenance + episodic event (`memory_formed`, `memory_superseded`,
   `memory_merged`, `memory_conflict`).
7. **Index** update (BM25 + vector if enabled).

Failure semantics: any step that fails leaves memory unchanged and writes a
`formation_rejected` episode with the reason; the agent loop continues.

### 6.2 Conflict and supersede rules

| Situation | Decision |
| --- | --- |
| same key, same content | duplicate → bump `updated_at`, no new record |
| same key, different content, newer + explicit user statement | supersede: old → SUPERSEDED, new ACTIVE |
| same key, different content, model-inferred | CONFLICT + PENDING_CONFIRMATION, both retrievable |
| different key, contradictory claim | mark both CONFLICT, surface in review queue |
| temporal change ("now uses Rust") | old `valid_until=now`, new `valid_from=now`, both retained |

### 6.3 Lifecycle and forgetting

Score = `importance × usage × recency × confidence`, but with hard floors:

* `memory_type=preference|decision` and `importance>=4` are never auto-deleted.
* `ARCHIVED` after `N` days unused unless pinned; deletion is an explicit admin action.
* `valid_until` in the past → retrieval penalty, not deletion.
* usage counter increments on retrieval selection (not on mere inclusion in a prompt).
* Review queue (`PENDING_CONFIRMATION`, `CONFLICT`) is exposed via API/CLI and the dashboard.

### 6.4 Explainability contract

`explain(memory_id)` returns: origin event id, evidence spans, forming rule/model, supersede
chain (`supersedes` / `superseded_by`), merge parents, retrieval history (count, last used,
which requests). `explain_query(query)` returns per candidate: score components, whether it was
selected or omitted and why. This is the AURA-compatible trace: decision → memory → evidence.

---

## 7. Out-of-the-box experience

```
pip install nanomemory          # (or uv pip install)
nanomemory init                 # creates storage, index, default config; no API key
nanomemory start                # API + dashboard, localhost, single worker
nanomemory doctor               # self-check: storage, locks, schema, index, provider
```

```python
from nanomemory import Memory

memory = Memory()  # auto-init, local files, no keys
memory.remember("I prefer Python.", user_id="u1", source="chat")
hits = memory.recall("Which language do I prefer?", user_id="u1", k=5)
memory.explain(hits[0].id)
```

Modes: `lite` (BM25 + rules, no embeddings, no LLM), `standard` (adds embeddings/rerank if a
provider is configured), `advanced` (multi-user, SQLite, ANN). Default: **lite**, and it must
work with zero configuration. Provider keys are optional everywhere; nothing in the default
path calls a model.

CLI/SDK are thin wrappers over `MemoryManager`; they must not reimplement policy.

---

## 8. Host independence

| Surface | Role | Rule |
| --- | --- | --- |
| Python SDK | primary embedding path | no DSH imports |
| REST | cross-language | existing FastAPI, versioned |
| MCP | one adapter among many | `mcp_server.py` stays a thin protocol shim |
| DSH plugin | convenience packaging | no product logic; it only mounts the MCP server |
| CLI | ops/migration | `init/start/doctor/migrate/export/import` |

`memory belongs to the user`: export/import (JSON + JSONL) is a first-class, tested path, and
switching model or host must not touch storage.

---

## 9. Migration and compatibility

1. `LONG_TERM_SCHEMA_VERSION = 2`; loader upgrades v1 records in memory, migration writes
   atomically under the maintenance lock with a backup (existing machinery).
2. Episode schema unchanged; new event types are additive.
3. Retrieval API keeps `RetrievalResult` fields; new fields (`explanation`, `omitted_reason`)
   are optional.
4. v1 JSON stays the default backend in P0/P1; SQLite is opt-in in P2 (`NANOMEMORY_BACKEND`).
5. Rollback: keep `schema_version` and a `migrate --down` path for v2→v1 (lossy: v2-only fields
   dropped into `provenance.raw`), documented.

---

## 10. Evaluation: gates, not vibes

Datasets (versioned, hashed, in `benchmarks/datasets/`):

* `memory_bench_long.json` — context/token efficiency (existing),
* `formation_bench.json` — new: labelled turns → expected records (precision/recall of formation),
* `conflict_bench.json` — new: sequences that must produce supersede/conflict decisions,
* `temporal_bench.json` — new: facts valid in windows, queries crossing boundaries,
* `lifecycle_bench.json` — new: archive/forget outcomes after simulated time.

Metrics and gates:

| Phase | Metric | Gate |
| --- | --- | --- |
| P0 | fact recall (long dataset) | **≥ 0.75** (from 0.39), full-history recall 1.0 |
| P0 | per-query tokens | ≤ full history (`saving_rate ≥ 0`) |
| P0 | safety suite | 271 existing tests stay green |
| P1 | formation precision / recall | ≥ 0.90 / ≥ 0.70 on labelled set |
| P1 | duplicate rate | < 2 % on repeated ingestion |
| P1 | conflict resolution accuracy | ≥ 0.85 |
| P1 | provenance coverage | 100 % of records have evidence |
| P2 | install→first recall | ≤ 5 min on a clean host, zero keys |
| P2 | migration round-trip | byte-identical export/import, v1→v2→v1 documented |
| P3 | relation/temporal queries | ≥ 0.70 exact-answer accuracy |
| P4 | multi-user isolation | no cross-user read in adversarial tests |

Anti-gaming rules: never report token savings without recall and task success; mock results are
labelled; every gate needs a raw artifact (`reports/`, `benchmarks/results/`).

---

## 11. Phased build spec

### P0 — Retrieval quality (highest priority)

Scope: `retrieval/{bm25,fusion,rerank,router}.py`, refactor `memory_retriever.py`,
`storage/index.py`, retrieval eval harness + report.

Tasks:
1. Pure-Python BM25 index over `MemoryRepository` records (incremental updates, thread-safe
   read snapshot).
2. RRF fusion of BM25 + optional vector + graph candidates; per-intent weights from `router.py`.
3. Reranker feature model (term coverage, entity match, type match, recency, importance,
   confidence, conflict penalty); no LLM.
4. Replace the flat `MIN_RELEVANCE_SCORE`/8-item cap with budget-aware packing and a
   `recall_floor` warning when the top score is weak (surface "low confidence" instead of
   silently answering).
5. Retrieval explanations: score components + omit reasons.
6. Eval: `benchmarks/retrieval_eval.py` printing recall@k, precision@k, token cost, and a
   per-cause breakdown (present/outranked/lexical-gap).

Exit criteria: recall ≥ 0.75 on long dataset, tokens ≤ full history, explanations present,
existing tests green, artifacts committed.

Estimate: 3–5 days.

### P1 — Cognitive memory

Scope: `memory/`, `formation/`, `consolidation/`, `lifecycle/`, `explain/`, schema v2.

Tasks: extractor (rules + optional LLM enrichment), classifier, admission gate, dedupe,
supersede/conflict resolver, lifecycle scheduler, provenance + explain APIs, formation and
conflict benchmarks.

Exit criteria: gates in §10 for P1; no LLM-only write path; review queue exposed.

Estimate: 8–12 days.

### P2 — Product experience

Scope: `sdk.py`, `cli.py`, `storage/sqlite_repository.py`, packaging, docs, doctor.

Tasks: `nanomemory init/start/doctor/migrate/export/import`, SDK (`Memory`), zero-key defaults,
SQLite backend + FTS5, export/import, quickstart in README with a 5-minute path.

Exit criteria: clean-host install→recall in ≤5 min (or documented blocker), zero-key lite mode
tested, migration round-trip test.

Estimate: 5–7 days.

### P3 — Advanced memory

Entity extraction/aliases, relation store, graph retrieval, temporal windows, project
timelines, profile rollups.

Estimate: 10–15 days.

### P4 — Production

Multi-user isolation, permissions, shared rate limit, SQLite/ANN at scale, observability,
retention policies.

Estimate: 10+ days.

---

## 12. Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Embedding cost/privacy | default off; local/hashing backends; cache; same key policy as LLM |
| LLM extraction hallucination | rules decide admission; LLM output is evidence-tagged and reviewable |
| SQLite/vec portability on Windows + Python 3.12 | P0/P1 stay JSON; SQLite only in P2 with fallback |
| Bench overfitting | separate formation/conflict/temporal sets; gates include safety regression |
| Install blocker on this host (0700 dirs, MAX_PATH) | verify on a normal filesystem; `nanomemory doctor` detects both |
| Scope creep | each phase has exit gates; no phase starts before the previous gate passes |

Open questions for the maintainer:
1. Default embedding backend for P1 (`openai_compatible` vs bundled local model vs none)?
2. Is multi-user a 2.0 goal or 3.0 (affects storage layout and API auth)?
3. Should conflict resolution ever auto-confirm a model inference, or always queue it?

---

## 13. On "validate with Mem0 first"

The advice is half right, and the half it gets wrong matters here.

**Agree:** an A/B (memory vs no memory) on task success is the only honest way to prove value;
recall is a means, not the goal. A cheap external baseline is worth having.

**Disagree:** this project already knows the failure and its cause — 39 % recall with a
single lexical scorer, 17/17 misses at retrieval, 8/28 with no lexical overlap. Running Mem0
first would answer "is memory useful for some app", not "can this engine meet its own bar".
It would also import a different consistency model (ADD-only, deferred conflicts) that
contradicts the security/provenance work already validated here.

**Compromise (cheap, non-binding):** add `benchmarks/compare_external.py` with an adapter
interface and a Mem0 adapter as an *optional* smoke comparison, run once, record the result in
`docs/BENCHMARKS.md`, and keep it out of the default test path. It costs ~1 day and gives the
user the external reference point without forking the architecture.

---

## 14. Five principles → engineering constraints

| Principle | Constraint in code |
| --- | --- |
| Memory belongs to the user | export/import is first-class; no host-specific storage; no silent cloud calls |
| Every memory has provenance | admission rejects records without an evidence span; `explain()` mandatory |
| Updates are traceable and reversible | supersede never deletes; every change writes an episode; `restore`/`undo` by id |
| Retrieval quality > quantity | recall gate ≥0.75 before any "smart" feature ships; usage counters drive lifecycle |
| Users need not understand internals | zero-key lite mode; defaults decide everything; `doctor` explains failures in plain language |

---

## 15. First PR (concrete)

Branch: `feat/retrieval-v2`.

Files added: `nanomemory/retrieval/{__init__,bm25,fusion,rerank,router}.py`,
`benchmarks/retrieval_eval.py`, `tests/unit/test_bm25.py`, `tests/unit/test_fusion.py`,
`tests/unit/test_router.py`, `tests/integration/test_retrieval_recall.py`.

Files changed: `core/memory_retriever.py` (delegate to the pipeline), `core/long_term_memory.py`
(expose `iter_all()`), `docs/ARCHITECTURE_2.0.md` (this file), `docs/BENCHMARKS.md` (recall
table).

Acceptance on the PR: recall ≥0.75 with BM25+rerank only (no embeddings), token cost ≤ full
history, all existing tests green, new eval artifact committed. Embeddings then attach as an
additional candidate source behind the same interface.
