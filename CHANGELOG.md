# Changelog

All notable changes to this project. Dates are ISO-8601.

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
- Dockerfile (non-root, stdlib healthcheck), docker-compose, `.env.example`, scripts,
  documentation and verification artifacts under `reports/`.

### Known limitations

- Local single-user MVP; sessions isolate files, not users.
- Single worker; the in-memory rate limiter is per-process.
- Keyword retrieval only; measured fact recall on the long-history benchmark is 39 %.
- No directory `fsync` on Windows; MAX_PATH applies.
- Docker verification depends on a Docker daemon being available in the environment.

[0.1.0]: https://example.invalid/nanomemory/releases/0.1.0
