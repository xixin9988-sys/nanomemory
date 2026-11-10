# NanoMemory Code Review

Reviewer role: independent engineering review of the implementation described in
`IMPLEMENTATION_PLAN.md`, performed against the v1.6 specification. This is a **self-review of
the delivered tree** (there was no separate Claude Code author in this environment); it is
written in the reviewer's format on purpose so the findings can be checked line by line.

## Executive Summary

The core memory engine is real: 258 tests pass (2 platform skips), the transaction engine is
exercised against injected failures and real subprocesses, the API and dashboard run, and the
benchmark produces reproducible artifacts. The two correctness bugs found during review were
fixed and covered by regression tests (`read_text_file` could read the memory storage tree;
deterministic compression recorded truncation without applying it).

Three things prevent a "production ready" claim and are reported as such:

1. `pip install -e ".[dev]"` could not be executed on this host (build tools always create
   mode-0700 temp directories, which this machine cannot re-open); the README command is
   therefore **NOT VERIFIED here**, even though the package layout and discovery are verified.
2. Docker build / healthcheck / volume persistence were **not executed** (no usable Docker
   daemon in this environment) — **NOT VERIFIED**.
3. Retrieval quality is weak on a realistic long-history benchmark: NanoMemory saves 67 % of
   per-query tokens but recalls only 39 % of required facts versus 100 % for full history.

## Critical Issues

None outstanding. The following was critical and is **fixed**:

### C-1 (fixed) – The file-reading tool could read memory storage files

- **Location:** `nanomemory/core/tools.py::_read_text_file`
- **Description:** the tool confined paths to `read_tool_allowed_root` (the project root) and
  an extension allowlist, but did not exclude `storage/`. A model could therefore read
  `storage/working/<other-session>/memory.md`, `memory.meta.json`, `knowledge.json` and
  `usage.jsonl`, bypassing the retrieval contract and leaking other sessions' data.
- **Evidence:** a tool context rooted at the project with a `storage/working/abc/memory.md`
  file returned `success=True` before the fix.
- **Impact:** cross-session confidentiality break inside a single user's machine; also lets
  the model read metrics/metadata it should only see through the API.
- **Fix:** resolved paths are compared against `settings.storage_root` and rejected with
  `path_not_allowed`. Regression test:
  `tests/unit/test_agent_tools_providers.py::test_read_text_file_blocks_memory_storage`.
- **Priority:** Critical (fixed).

## High Priority Issues

### H-1 – Retrieval recall is too low for the advertised token savings

- **Location:** `nanomemory/core/memory_retriever.py::_score_record`, `MIN_RELEVANCE_SCORE`,
  `MAX_LONG_TERM_ITEMS`
- **Description:** keyword overlap plus tag/category bonuses, an 8-item cap and a hard budget.
  On `memory_bench_long.json` this retrieves the required facts in only 39.3 % of cases, while
  full history scores 100 %.
- **Evidence:** `reports/phase7_token_benchmark.txt`, summary
  `benchmarks/results/mock-20261010T044413Z-922c814_summary.json` (`fact_recall_rate` 0.3929
  vs 1.0).
- **Impact:** the system is token-efficient but not yet quality-equivalent. A README claim of
  savings without this number would be misleading, so both are reported together.
- **Suggested fix:** IDF/BM25 weighting over the store, per-case tag or entity matching,
  optional embeddings behind the same budget interface, and a recall floor that refuses to
  answer with obviously insufficient context.
- **Priority:** High (open).

### H-2 – Installation path not verified on this host

- **Location:** packaging (`pyproject.toml`), environment.
- **Description:** `python -m pip install -e ".[dev]"` and even a plain wheel build fail with
  `PermissionError` on `tempfile.mkdtemp` directories. `mkdir(mode=0o700)` on this Windows
  host produces a directory the creating user cannot reopen, and `chmod` on those paths is
  denied. pip and setuptools both create 0700 temp directories *inside their own
  subprocesses*, so no shim in the parent process can fix it; a `subst` short-path attempt was
  also denied.
- **Evidence:** repeated pip/uv/setuptools attempts; the exact errors are recorded in the
  delivery report. By contrast, imports, packaging discovery (`find_packages`) and all tests
  work from the source tree.
- **Impact:** the acceptance criterion "a clean environment can install per README" is
  unproven here. The `pyproject.toml` is a conventional setuptools flat-layout configuration
  with no custom build steps, so the risk is environmental, but it is not verified.
- **Suggested fix (operator):** run the README install on a normal NTFS/APFS/ext4 path (or
  with long paths enabled); no code change expected.
- **Priority:** High (open, environment).

### H-3 – No directory fsync on Windows

- **Location:** `nanomemory/core/fsio.py::fsync_dir`
- **Description:** returns `False` on Windows because CPython cannot fsync a directory there.
  The journal still makes recovery deterministic, but the rename itself is not proven durable
  across power loss.
- **Evidence:** `test_fsync_dir_reports_platform_support` asserts `False` on Windows; the
  limitation is documented in `docs/SECURITY.md` and `docs/ARCHITECTURE.md`.
- **Impact:** a hard power loss immediately after `os.replace` could leave the previous
  version visible; never a half-written file, and never a silently corrupted state.
- **Suggested fix:** platform-specific directory handle fsync (`CreateFileW` with
  `FILE_FLAG_BACKUP_SEMANTICS`), or document POSIX as the durable target.
- **Priority:** High (platform limitation, documented).

## Medium Priority Issues

### M-1 – Rate limiting is per-process only

- **Location:** `nanomemory/api/deps.py::RateLimiter`
- **Description:** an in-memory fixed-window limiter. With `worker_count > 1` the health
  endpoint reports `rate_limit_not_shared` and responses advertise
  `X-NanoMemory-RateLimit-Scope: per-process`; the service does not claim a global limit.
- **Impact:** acceptable for the supported single-worker deployment; not a global limit.
- **Fix:** shared backend (SQLite with transactional counters) as planned.
- **Priority:** Medium.

### M-2 – Windows MAX_PATH constrains deep storage roots

- **Location:** `nanomemory/core/fsio.py` temp names + per-transaction directories.
- **Description:** the mandated temp name is ~75 characters; combined with
  `.txn/<32 hex>/` a deep project path exceeds 260 characters and writes fail with
  `FileNotFoundError`.
- **Evidence:** the benchmark initially failed under `benchmarks/results/<run_id>/work/...`;
  it now uses a short `.bw/<8 hex>` work directory (documented in `docs/BENCHMARKS.md`).
- **Impact:** users with deep paths must shorten them or enable long-path support.
- **Fix:** prefix paths with `\\?\` on Windows or validate root length at startup.
- **Priority:** Medium.

### M-3 – `export_snapshot` truncates the episode list

- **Location:** `nanomemory/core/memory_manager.py::export_snapshot`
- **Description:** exports at most 1000 episodes (the read limit), silently.
- **Impact:** an export is not a complete archive for large logs.
- **Fix:** paginate internally or state the limit in the export payload and API docs.
- **Priority:** Medium.

### M-4 – Restore refuses with `restore_failed` (500) when a transaction is pending

- **Location:** `nanomemory/core/backup.py::_assert_no_pending_transactions`
- **Description:** refusing is correct, but the condition is a conflict, not a server error.
- **Impact:** clients cannot distinguish "retry after recovery" from "restore is broken".
- **Fix:** return 409 with `code=session_recovery_required` or a dedicated code.
- **Priority:** Medium (API semantics).

### M-5 – `GET /api/v1/memory/working` can create the default session

- **Location:** `nanomemory/core/memory_manager.py::resolve_session`
- **Description:** in single-session mode the default session is ensured on resolution, so a
  read request can create directories.
- **Impact:** a read has a (benign, idempotent) write side effect.
- **Fix:** ensure the default session during startup only.
- **Priority:** Medium.

## Low Priority Issues

- **L-1** `B008` is ignored for `nanomemory/api/**` because FastAPI requires `Query()` in
  defaults (documented in `pyproject.toml`).
- **L-2** Two file-symlink tests are skipped on this host (symlink creation privilege is
  unavailable). Directory junctions are covered instead, including the reparse-point path
  used by attackers.
- **L-3** `docs/BENCHMARKS.md` and README quote a single 10-case run without confidence
  intervals; intentional, but the sample size is small.
- **L-4** The dashboard's benchmark page reads `benchmarks/results` directly. These are
  experiment artifacts, not storage files, and the dashboard never writes there; the
  distinction is documented in that component.

## Security Findings

| Check | Result |
| --- | --- |
| Secrets in repository | `reports/sensitive_scan.py` finds none outside documented fake fixtures |
| Path traversal | rejected; tests include `..`, absolute paths, Windows case/backslash variants |
| Symlink / junction | rejected in controlled directories, including the health directory |
| Memory storage via file tool | **fixed** (Critical C-1) |
| Untrusted memory vs system prompt | separated, escaped, injection fixtures tested |
| Admin token | required, constant-time compare, disabled by default, not logged |
| Log redaction | handler-level; tests cover message, extras, exception text, mock keys |
| Error bodies | no stack traces, no paths, no secrets; request id only |
| CORS | closed unless explicitly allowlisted |
| Docker | non-root user, no baked credentials; **not executed here** |

Residual risks: a local user who can write the storage tree can tamper with memory files
(detected as `recovery_required`, not prevented); sessions isolate files, not identities;
`st.session_state` in the dashboard is not an authentication boundary.

## Test Coverage Gaps

- **OpenAI-compatible adapter: 21 % coverage.** Only configuration failure is tested; the live
  request/error-classification paths need a contract test against a stub server.
- **Docker:** build, healthcheck and volume persistence are untested (no daemon).
- **Directory fsync on Windows:** untestable on this host (documented, not mocked away).
- **Migrations:** only the `long_term` v0→v1 path exists, so cross-format migration is
  unproven.
- **Dashboard:** AppTest covers startup, Overview data, Benchmark empty/data state and the
  token helper contract; deeper widget interactions (forms, destructive flows) are not
  exercised.
- **Concurrency:** three-process updates, two-process recovery and lock-timeout scenarios are
  covered; combined backup+update and migration-vs-API races are covered in-process only.

## Benchmark Validity

- Same tasks, same system prompt, same context scaffold, tool schemas excluded from all
  strategies — the only variable is memory content.
- Dataset SHA-256, seed and commit hash are recorded in every artifact; raw per-case results
  are kept, not just aggregates.
- Pre-processing cost is counted separately and in `total_tokens_with_preprocessing`; the
  summary strategy's advantage is shown to be amortisation, not free compression.
- Fact recall is deterministic (NFKC + casefold + declared match terms), not an LLM judge.
- `task_success` is `null` without human labels; the mock provider explicitly measures context
  coverage and is labelled as such everywhere.
- **Weakness:** 10 cases, one run, mock provider. No statistical claim is made.

## Recommended Fixes (ordered)

1. Improve retrieval quality (IDF/BM25 then hybrid embeddings) and re-run the long-history
   benchmark; target recall ≥ 0.8 at a comparable token budget.
2. Add a stub-server contract test for `OpenAICompatibleProvider` (auth, rate limit, timeout,
   invalid JSON, usage parsing).
3. Verify the install path and the Docker flow on a normal filesystem/daemon and record the
   artifacts.
4. Change the pending-transaction restore refusal to a 409 with a recovery-specific code.
5. Add pagination/limit reporting to `export_snapshot`.
6. Add Windows extended-length path handling (or a startup warning when the storage root is
   too deep).

## Final Assessment

- **Delivered:** a coherent, tested, documented local memory engine whose P0 behaviours
  (cross-process locks, sidecar transaction journal, in-lock rollback with hash verification,
  startup recovery order, quarantine isolation, backup manifests, migration maintenance mode,
  secret scanning, path containment, budgets, tool allowlist) are implemented and evidenced.
- **Not production ready:** install verification and Docker verification are NOT VERIFIED, and
  retrieval quality is materially below full history on the benchmark.
- **Status:** Local single-user MVP. The README, this review and the final report all state
  that limitation instead of implying more.
