# Changelog

All notable changes to this project. Dates are ISO-8601.

## [0.2.0] - 2026-11-10

Memory-quality and cognitive-memory release. Storage, transactions, locks and recovery are
unchanged; the cognitive layer sits on top of them.

### Added

- **Memory model v2**: `memory_type` (`fact / preference / event / procedure / relationship /
  autobiographical`), nested `provenance` (`source_kind`, `source_ref`, `derived_from`,
  `created_by`, `note`), `valid_from` / `valid_until`, supersede chain (`supersedes`,
  `superseded_by`), recall utility (`recall_count`, `last_recalled_at`) and the statuses
  `ACTIVE / SUPERSEDED / CONFLICT / ARCHIVED / PENDING_CONFIRMATION`.
  `knowledge.json` migrates v1 → v2 automatically through the existing migration journal.
- **Automatic memory formation** (`core/memory_formation.py`): deterministic extraction and
  classification, per-type admission rules, dedupe/conflict detection, supersede on newer
  explicit statements, and audit events. A model-derived memory is stored as
  `pending_confirmation` and never presented as a user-stated fact.
- **Hybrid retrieval v2** (`core/memory_retriever.py`): BM25 + character-trigram semantic +
  one-hop relation expansion + query-intent weights (`preference / project_state /
  decision_history / timeline / general`) + reranking, with per-signal explanation and an
  `insufficient_context` fallback based on content signal floors.
- **Lifecycle** (`core/lifecycle.py`): retention = importance × utility × recency × confidence;
  archives instead of deleting; stable preferences are protected.
- **Explainability**: `explain(memory_id)` returns provenance, supersede chain and the audit
  events that created or replaced the memory.
- **SDK**: `from nanomemory import Memory` with `remember / recall / forget / confirm / reject /
  explain / lifecycle / status`; lite mode needs no API key.
- **CLI**: `nanomemory init|start|status|remember|recall|explain|lifecycle|doctor`, exported as
  a console script and available as `python -m nanomemory`.
- **REST**: `POST /api/v1/memory/remember`, `GET /api/v1/memory/recall`,
  `GET /api/v1/memory/{id}/explain`, `POST /api/v1/memory/{id}/confirm|reject`,
  `POST /api/v1/memory/lifecycle/run`.
- **Retrieval benchmark** (`benchmarks/retrieval_benchmark.py`): Fact Recall@K, Precision@K,
  Provenance Coverage, temporal-update accuracy and conflict-detection accuracy.

### Changed

- Fact recall on the long-history benchmark: **0.39 → 0.86** at the same 67 % token saving
  (mock mode, same dataset). Token use stayed flat; the gain came from retrieval, not from
  loading more memory.
- `README.md` restructured around the 2.0 product surface; `docs/NANOMEMORY_2.0.md` records the
  architecture and roadmap.

### Fixed

- Health payload now reports the real token estimator name instead of a stale hard-coded one.
- Generated artifacts (coverage, JUnit, benchmark metadata, report headers) no longer embed
  machine-specific absolute paths or the local hostname.

## [0.1.0] - 2026-11-10

First implementation of the NanoMemory 2.0 specification (v1.6), delivered as a local
single-user MVP.

### Added

- Three-layer memory: Working (Markdown + JSON sidecar), Long-term (`knowledge.json`,
  per-record versions, 5000-record cap) and Episodic (`events.jsonl`, append-only).
- Crash-safe Working Memory transactions: per-transaction subdirectories, durable old
  snapshots, atomic prepared journal, in-lock rollback with hash verification, fail-closed
  `recovery_required` state.
- Startup recovery in the mandated order: maintenance/migration recovery first, then
  per-session recovery; orphan transaction directories and orphan temp files are handled with
  strict allowlists.
- Cross-process resource locks (`maintenance → backup → working → long_term → episodic →
  metrics`) with timeouts, canonical ordering and instrumentation.
- Backup/restore with manifests, per-file SHA-256, pre-restore snapshots, confirmation and
  admin-token protection, and version-regression protection.
- Schema migration with a durable maintenance marker, migration journal, backup rollback and
  startup recovery; legacy `knowledge.json` v0 → v1 migration.
- Secret scanning (explicit credential patterns + entropy candidates with allowlists) and
  log redaction across handlers.
- Retriever with explainable scores and a hard token budget; context builder with untrusted
  data separation, XML escaping and total request budget; deterministic and opt-in
  LLM-assisted compression with a fidelity diff.
- Provider abstraction with a deterministic mock provider (echo/scripted/fail_json/timeout/
  rate_limit) and an OpenAI-compatible adapter with classified errors.
- Bounded agent loop with iteration, wall-clock, token, tool-call and cost budgets, tool
  allowlist, schema validation and proposal-only memory updates.
- FastAPI surface with a uniform error envelope, request ids, rate limiting, session rules,
  maintenance mode, admin auth and OpenAPI docs.
- Streamlit dashboard (Overview, Memory Explorer, Token Analytics, Benchmark, Agent Console)
  that talks to the API only, with one-shot admin token entry.
- Benchmark harness comparing Full History, Summary and NanoMemory with reproducible
  artifacts, dataset hashes and mock/real labelling.
- DeepSeek Harness plugin (dsh-plugin/) plus a stdio MCP server
  (
anomemory/mcp_server.py): seven mcp__nanomemory__* tools covering health, Working
  Memory (version-checked updates), long-term add/search, episodes and the bounded agent loop.
- Dockerfile (non-root, stdlib healthcheck), docker-compose, `.env.example`, scripts,
  documentation and verification artifacts under `reports/`.

### Known limitations

- Local single-user MVP; sessions isolate files, not users.
- Single worker; the in-memory rate limiter is per-process.
- Keyword retrieval only; measured fact recall on the long-history benchmark is 39 %.
- No directory `fsync` on Windows; MAX_PATH applies.
- Docker verification depends on a Docker daemon being available in the environment.

[0.1.0]: https://example.invalid/nanomemory/releases/0.1.0
