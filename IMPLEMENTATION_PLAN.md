# NanoMemory 2.0 — IMPLEMENTATION_PLAN

- 文件版本：1.0
- 依據規格：`NanoMemory_Development_Prompt_v1.6.txt`（§0–§36）
- 產品定位：A Lightweight, Persistent & Token-Efficient Memory Engine for AI Agents
- 狀態定位：**Local single-user MVP**（未實作多使用者隔離）

---

## 0. PHASE 0：環境檢查結果（實測）

| 項目 | 結果 |
| --- | --- |
| 作業系統 | Windows（10.0.26200 級別）、PowerShell |
| 工作目錄 | `<workspace>`（使用者本機路徑，不記錄於本文件） |
| 既有使用者內容 | 有（其他專案目錄與檔案），**因此本專案建立於子目錄 `NanoMemory/`，不覆寫任何既有檔案** |
| Python | 系統 `python` 3.14.8；專案虛擬環境 `NanoMemory/.venv` 使用 **CPython 3.12.15**（符合規格 ≥3.11，且 3.12 對 streamlit/pandas 有完整 wheel 支援） |
| 已安裝（venv 實測） | fastapi 0.162.2、pydantic 2.14.3、pydantic-settings、uvicorn 0.54、streamlit 1.67.0、pytest 9.2.0、httpx、filelock、openai 3.x、ruff、pytest-cov、pytest-asyncio、hypothesis |
| 網路 | PyPI 可用（pip/uv 走系統代理），GitHub 直連不可用（與本專案無關） |
| Docker | 尚未確認可用；若不可用，所有 Docker 驗收項目標示 NOT VERIFIED（不假稱通過） |
| Git | `E:\git\cmd\git.exe` 可用；本專案將建立獨立 repo（`git init`）以產生 commit hash 供 benchmark/報告使用 |

### 0.1 既有檔案處理（§22.26）
- 目標目錄 `NanoMemory/` 於 PHASE 0 前**不存在**，無覆寫風險。
- 專案內所有產生檔案均為新建；不刪除、不搬移工作目錄其他使用者內容。
- 破壞性操作（restore / 永久刪除 / 清空記憶）僅存在於執行期 API，且預設停用（需 `NANOMEMORY_ADMIN_TOKEN`）。

### 0.2 布局決策（§26.3、§29.9）
- **固定 flat layout**：唯一應用程式 package root 為 `nanomemory/`。
- 所有 package initializer 實檔名為 `__init__.py`（§33.1、§35.3）。§3/§22.19 中因換行/排版而消失的雙底線，一律以 `__init__.py` 為準。
- 不安裝 `core/`、`providers/`、`api/`、`dashboard/` 於 package root 之外的平行 package。
- `pyproject.toml` package discovery、Uvicorn import string、Streamlit script path、pytest import path 全部以 `nanomemory` 為根：
  - API：`uvicorn nanomemory.api.main:app --reload`
  - Dashboard：`streamlit run nanomemory/dashboard/app.py`
  - 安裝：`pip install -e ".[dev]"`

---

## 1. 最終目錄結構（實際建立）

```
NanoMemory/
├── nanomemory/
│   ├── __init__.py                     # 版本、公開 API
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                   # pydantic-settings（NANOMEMORY_*）
│   │   ├── errors.py                   # 統一錯誤碼 / 例外型別
│   │   ├── logging_config.py           # 結構化日誌 + redaction handler
│   │   ├── redaction.py                # 日誌/字串 redaction 規則
│   │   ├── paths.py                    # 路徑限制、symlink 檢查、resolve
│   │   ├── fsio.py                     # atomic write、fsync、temp 命名、孤兒掃描
│   │   ├── locks.py                    # LockManager（filelock，固定順序、timeout、instrumentation）
│   │   ├── memory_models.py            # Pydantic 模型（WM/LTM/Episode/Journal/…）
│   │   ├── secret_scanner.py           # §29.7 + §35.4
│   │   ├── memory_validator.py         # 提案驗證 + 秘密掃描 + 欄位長度
│   │   ├── working_memory.py           # sidecar + per-txn 交易狀態機（§28.1/§33.2/§34.1/§34.2/§36.1/§36.2）
│   │   ├── recovery.py                 # 啟動順序（§36.3）、session recovery（§33.3/§35）
│   │   ├── long_term_memory.py         # knowledge.json、per-record version、容量、衝突
│   │   ├── episodic_memory.py          # events.jsonl、quarantine、correction（§22.4/§24.6/§29.6）
│   │   ├── backup.py                   # manifest/SHA-256/restore（§22.5/§28.3）
│   │   ├── migrations.py               # schema migration + maintenance lock/marker（§26.2/§28.2/§34.4）
│   │   ├── memory_manager.py           # Memory Controller façade
│   │   ├── token_counter.py            # exact / estimated / unknown
│   │   ├── memory_retriever.py         # 關鍵字/tag/category/importance/recency + budget
│   │   ├── context_builder.py          # untrusted memory payload（§25.2/§29.3）
│   │   ├── memory_compressor.py        # deterministic + llm_assisted（§22.15）
│   │   ├── tools.py                    # ToolSpec / RegisteredTool / Registry（§25.3/§29.4）
│   │   ├── budget.py                   # agent 總預算（§22.10）
│   │   ├── metrics.py                  # usage.jsonl + rotation（§22.16/§30.1/§35.6）
│   │   ├── agent_loop.py               # §8
│   │   └── health.py                   # /health 語義（§25.7/§30.7）
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py                     # interface + 錯誤分類
│   │   ├── mock_provider.py            # deterministic（§25.4）
│   │   └── openai_compatible.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py                     # app factory、lifespan、middleware
│   │   ├── errors.py                   # 統一錯誤 envelope
│   │   ├── schemas.py
│   │   ├── deps.py                     # session / admin token 解析
│   │   ├── rate_limit.py               # §30.3 / §35.7
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── health.py
│   │       ├── sessions.py
│   │       ├── memory.py
│   │       ├── agent.py
│   │       ├── metrics.py
│   │       └── admin.py
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py                      # Streamlit entry（僅經 API）
│       ├── client.py                   # API client
│       └── components/
│           ├── __init__.py
│           ├── overview.py
│           ├── memory_explorer.py
│           ├── token_analytics.py
│           ├── agent_console.py
│           └── benchmark_view.py
├── storage/
│   ├── working/  long_term/  episodic/  backups/  metrics/
│   ├── .locks/  .health/  .maintenance.json(動態)
│   └── .gitkeep
├── benchmarks/
│   ├── __init__.py
│   ├── dataset.py  runner.py
│   ├── token_benchmark.py  task_benchmark.py
│   ├── datasets/  results/
├── tests/
│   ├── unit/  integration/  fixtures/
├── scripts/
│   ├── init_storage.py
│   ├── run_api.py
│   ├── run_dashboard.py
│   ├── health_probe.py
│   └── cleanup_orphans.py
├── docs/
│   ├── ARCHITECTURE.md  MEMORY_FORMAT.md  API.md  BENCHMARKS.md  SECURITY.md
├── reports/                            # 每個 Phase 的實際命令輸出（txt / JUnit XML / coverage XML / JSON）
├── .env.example  .gitignore  .dockerignore
├── Dockerfile  docker-compose.yml
├── pyproject.toml  README.md  IMPLEMENTATION_PLAN.md  CHANGELOG.md  LICENSE
└── REVIEW_REPORT.md
```

---

## 2. 架構總覽

```
Streamlit Dashboard ──HTTP──> FastAPI ──> MemoryManager (Controller)
                                             │
                    ┌────────────────────────┼─────────────────────────┐
                    │                        │                         │
             WorkingMemoryStore      LongTermMemoryStore        EpisodicMemoryStore
             (md + meta sidecar,     (knowledge.json,           (events.jsonl, append-only,
              per-txn journal)        per-record version)        quarantine + correction)
                    │                        │                         │
                    └────────── Layout / Locks / AtomicIO / Migrations ─┘
                                             │
                        Retriever ─ ContextBuilder ─ Compressor
                                             │
                              AgentLoop ─ Providers(mock | openai_compatible)
                                             │
                     ToolRegistry（程式端白名單、schema、timeout、budget）
```

### 2.1 核心不變量
1. 記憶內容一律 **untrusted data**；只有 system message 承載指令。
2. 所有寫入經 `MemoryValidator`（schema/長度/來源/秘密掃描）。
3. 所有狀態變更在 **跨進程 OS-backed lock** 內完成（filelock）。
4. Working Memory 正文與 metadata 分離，版本只存在 `memory.meta.json`。
5. 任何非 committed 的交易都不得被當成已提交狀態。
6. 沒有原始產物的驗收項目不得標示 VERIFIED。

---

## 3. 資料格式

### 3.1 Working Memory（`storage/working/<session_id>/`）
- `memory.md`：人類可讀正文（Current Goal / Status / Relevant Context / Completed / In Progress / Blockers / Next Action）。
- `memory.meta.json`：`{schema_version, memory_version, session_id, updated_at, content_sha256, status}`。
- `.state.json`：session 執行狀態（`active` / `recovery_required` / `recovery_pending`）。
- `.txn/<txn_id>/`：`journal.json`、`new_memory.md`、`new_memory.meta.json`、`old_memory.md`、`old_memory.meta.json`、`journal.prepared.tmp.<pid>.<uuid4hex>`。
- `.txn/<txn_id>/journal.json` 欄位：`journal_schema_version, txn_id, session_id, old_md_sha256, old_meta_sha256, old_exists, new_md_sha256, new_meta_sha256, new_md_tmp, new_meta_tmp, old_md_snapshot, old_meta_snapshot, status(prepared|committed|rolled_back), started_at, completed_at`。

### 3.2 Long-term Memory（`storage/long_term/knowledge.json`）
- `{schema_version, updated_at, records: [...]}`；上限 5000 筆有效記憶（可調低）。
- record：`id, content, category, created_at, updated_at, source, source_type, confidence(low|medium|high), importance(1-5), tags[], status(active|archived|conflict|needs_review), trust_level, version, schema_version`。

### 3.3 Episodic Memory（`storage/episodic/events.jsonl`）
- 每行：`event_id, timestamp, session_id, event_type, summary, related_memory_ids[], source, outcome, schema_version`。
- 旁路檔：`events.corrupt.jsonl`（隔離，不動原檔）、`quarantine_acks.json`（acknowledgement 審計）。

### 3.4 Metrics（`storage/metrics/usage.jsonl`）
- `request_id, timestamp, provider, model, usage_source, prompt_tokens, completion_tokens, total_tokens, estimated_tokens, latency_ms, cost, cost_currency, session_id, benchmark, kind`。

### 3.5 Backups（`storage/backups/<backup_id>/`）
- `manifest.json`：`backup_id(UUID), created_at, schema_versions, product_version, files[{rel_path, sha256, size, memory_version}], label`。
- 保留策略：最近 N 份 + 每日一份。

---

## 4. Lock Matrix（§24.2 / §28.2 / §34.4）

全域固定取得順序（不得反向）：

| # | 資源 | 鎖檔 | 用途 |
| --- | --- | --- | --- |
| 0 | maintenance | `.locks/maintenance.lock` | migration / 全域維護 |
| 1 | backup | `.locks/backup.lock` | backup / restore |
| 2 | working  | `.locks/working-<session>.lock` | WM 讀取、驗證、交易、恢復 |
| 3 | long_term | `.locks/long_term.lock` | knowledge.json |
| 4 | episodic | `.locks/episodic.lock` | events.jsonl append/read/quarantine |
| 5 | metrics | `.locks/metrics.lock` | usage.jsonl append + rotation |

| 操作 | 需要的鎖（順序取得） | 持鎖範圍 | timeout 行為 |
| --- | --- | --- | --- |
| WM 讀取 | maintenance → working | 讀檔 + meta 驗證 | 503 `lock_timeout` |
| WM 更新（validate+apply） | maintenance → working | 讀版本 → 驗證 → snapshot → journal → replace → committed → audit | 503 `lock_timeout`；版本不符 409 |
| LTM 讀 | maintenance → long_term | 讀檔 | 503 |
| LTM 寫 | maintenance → long_term | 讀 → 驗證 → 衝突 → 容量 → 原子寫 → audit | 503 |
| Episodic append | maintenance → episodic | 損壞檢查 → append → fsync | 503；未處理 quarantine 預設拒絕 |
| Metrics append/rotate | maintenance → metrics | 檢查大小 → rotate → append | 503 |
| Health | health lock（專用短 timeout） | temp write/flush/replace/cleanup | 標記 degraded |
| Backup | maintenance → backup → working…metrics（短暫） | 建立一致快照 → 釋放資源鎖 → 寫 manifest | 503 |
| Restore | maintenance → backup → working…metrics | pre-restore snapshot → 驗證 → 替換 → 驗證 → audit | 503；admin token |
| Migration | maintenance → backup → working…metrics（全持） | marker → migrate → 驗證 → 清 marker | 任一鎖 timeout：反向釋放、無 marker、無 mutation |

- 所有鎖 timeout 預設 5 秒（`NANOMEMORY_LOCK_TIMEOUT_SECONDS`）。
- 解鎖一律反向。
- Inline snapshot 在 working 鎖內進行，不取 backup 鎖、不呼叫 backup service。
- Migration 的 marker 只在全鎖取得後以 atomic-write 建立。

### 4.1 Session 狀態機
`active` →（交易/回滾失敗）→ `recovery_required` →（啟動恢復成功）→ `active`。
`recovery_required` 期間：任何 API 取得 working 鎖後即檢查，立即釋放並回 503 `session_recovery_required`。

---

## 5. Working Memory 交易狀態機（§28.1 / §33.2 / §34.1–§34.3 / §36.1–§36.2）

```
[working lock 已取得]
 1. 讀 memory.meta.json → expected_version 檢查（不符→409 結束）
 2. mkdir .txn/<txn_id>
 3. 舊檔情況：old_exists ? (temp→fsync→hash→replace→dir fsync，兩個 snapshot 皆驗證) : old_exists=false
 4. 寫 new_memory.md / new_memory.meta.json（flush+fsync）→ 驗證 hash/session/version
 5. journal status=prepared：temp→fsync→parse 驗證→replace→.txn/<txn_id> fsync
 6. replace new→正式 memory.md；replace new meta→memory.meta.json（逐檔記錄成功）
 7. fsync session dir
 8. journal → committed（原子更新）
 9. audit event + retention 清理（僅 committed/rolled_back 子目錄）
```

失敗分支：
- 步驟 3–5 任一失敗 → 正式檔未動；`transaction_prepare_failed` audit；清理本 txn 子目錄；回 500。
- 步驟 6 任一 replace 失敗 → **同一把 working 鎖內**依 §34.3 驗證兩正式檔 hash（只允許 old/new/absent）→ 以耐久 old snapshot 還原兩檔 → 驗證 → journal=rolled_back → audit → 清理 → 釋放鎖 → 500 `txn_commit_failed`。
- 回滾失敗 / hash 未知 → 持久化 `recovery_required` + audit `recovery_required_entered` → 保留全部交易檔 → 釋放鎖 → 500 且 `details.session_state=recovery_required`。

啟動恢復（先 maintenance，後 session，皆持鎖）：committed → 驗 hash 後清理或 fail closed；prepared → 優先回滾；rolled_back → 冪等清理（§36.2）；journal 損壞 → quarantine + `recovery_required`；無 journal 的子目錄 → §36.5 孤兒清理或 fail closed；journal temp → §36.4。

---

## 6. 開發階段與交付

| Phase | 內容 | 主要驗收 |
| --- | --- | --- |
| 0 | 環境檢查、本計畫 | 本文件 |
| 1 | pyproject、config、errors、logging、paths、fsio、locks、package skeleton | import smoke、`__init__.py` 檢查、config/lock/fsio 單元測試 |
| 2 | 三層記憶 + 交易 + 恢復 + validator + secret scanner + backup + migrations + manager | §24/§28/§33/§34/§36 測試與 crash matrix |
| 3 | token counter、retriever、context builder、compressor | budget/escape/injection 測試 |
| 4 | providers、tool registry、agent loop | mock 全流程、budget、timeout、白名單 |
| 5 | FastAPI | endpoint 測試、錯誤格式、409/503/429 |
| 6 | Streamlit Dashboard（僅經 API） | AppTest 啟動與核心讀取 |
| 7 | Benchmark | 三策略、mock/real 區分、raw JSON + 報告 |
| 8 | 文件 + Docker | README 命令實跑；Docker 視環境標示 |
| 9 | 最終驗證 | pytest/ruff/import/API smoke/敏感資訊掃描，reports/ 產物 |

每階段把命令輸出寫入 `reports/`（txt + JUnit XML + coverage XML）。

---

## 7. 風險與對策

| 風險 | 影響 | 對策 |
| --- | --- | --- |
| Windows 不支援 directory fsync | durability 無法完全保證 | `fsync_dir` 回傳 False 並記錄；SECURITY.md 說明；測試以平台標記 |
| 多 worker 部署 | rate limit / 鎖假設失效 | v1 僅支援單 worker；啟動檢查，設定多 worker 時拒絕或標 degraded |
| Streamlit session_state 生命週期 | admin token 可能殘留 | 每次破壞性操作重新輸入；一次性 nonce widget key；文件說明不等於身份驗證 |
| 檔案鎖在雲端同步目錄 | 同步衝突風險 | 文件警告；storage 可設定到非同步目錄 |
| Token 估算偏差 | budget 低估 | 保守 80% margin + tokenizer 可替換 + 標示 estimated |
| Docker 環境不可用 | Docker 驗收無法通過 | 標示 NOT VERIFIED，不假稱通過 |
| 規模（5000 筆）效能 | 讀寫延遲 | 單檔 JSON、寫入時全量重寫屬已知設計；文件記錄上限與延遲 |

---

## 8. 規格條款 → 模組 → 測試 → artifact 追蹤矩陣

> Artifact 一律為 `reports/` 下實際產物（JUnit XML / coverage XML / 命令輸出）。沒有產物者標 NOT VERIFIED。

### 8.1 §22（v1.1 P0）
| 條款 | 模組/檔案 | 測試 | Artifact |
| --- | --- | --- | --- |
| 22.2 跨進程鎖 / 交易邊界 | `core/locks.py`, `core/working_memory.py` | `test_locks.py`, `test_working_txn_concurrency.py` | `reports/phase2_junit.xml` |
| 22.3 atomic write + fsync | `core/fsio.py` | `test_fsio_atomic.py` | 同上 |
| 22.4 JSONL 損壞隔離 | `core/episodic_memory.py` | `test_episodic_quarantine.py` | 同上 |
| 22.5 備份 manifest/restore | `core/backup.py` | `test_backup_manifest.py` | 同上 |
| 22.6 信任邊界 | `core/context_builder.py`, `core/tools.py` | `test_context_injection.py` | `phase3_junit.xml` |
| 22.7 秘密掃描 | `core/secret_scanner.py`, `memory_validator.py` | `test_secret_scanner.py` | `phase2_junit.xml` |
| 22.8 路徑/symlink | `core/paths.py` | `test_paths_symlink.py` | 同上 |
| 22.9 破壞性操作保護 | `core/migrations.py`, `api/routes/admin.py`, dashboard | `test_admin_token.py`, `test_restore_requires_admin.py` | `phase5_junit.xml` |
| 22.10 Agent 預算 | `core/budget.py`, `agent_loop.py` | `test_agent_budget.py` | `phase4_junit.xml` |
| 22.11 LTM 欄位語義 | `core/long_term_memory.py` | `test_long_term_conflict.py` | `phase2_junit.xml` |
| 22.12 Token budget 保守 | `core/memory_retriever.py`, `context_builder.py` | `test_retriever_budget.py` | `phase3_junit.xml` |
| 22.13 Schema migration | `core/migrations.py` | `test_migrations.py` | `phase2_junit.xml` |
| 22.14 Episodic correction | `core/episodic_memory.py` | `test_episodic_correction.py` | 同上 |
| 22.15 Compressor 保真 | `core/memory_compressor.py` | `test_compressor_fidelity.py` | `phase3_junit.xml` |
| 22.16 Metrics 持久化 | `core/metrics.py` | `test_metrics.py` | `phase2_junit.xml` |
| 22.17 Benchmark 公平性 | `benchmarks/runner.py` | `test_benchmark_scoring.py` | `benchmark_*.json` |
| 22.18 Dashboard 資料存取 | `dashboard/*`, `api/*` | `test_dashboard_app.py` | `phase6_junit.xml` |
| 22.19/22.23/22.24 | 全專案 | 全部 | `reports/phase*_junit.xml` |

### 8.2 §24（v1.2 P0）
| 條款 | 模組 | 測試 |
| --- | --- | --- |
| 24.1 initializer 拼法 | 全 package | `test_package_initializer_names` |
| 24.2 鎖粒度/順序/timeout | `core/locks.py` | `test_lock_order_matrix`, `test_lock_timeout` |
| 24.3 Session 來源 | `api/deps.py`, `core/config.py` | `test_session_resolution` |
| 24.4 Admin token | `api/deps.py`, `core/config.py` | `test_admin_token_required_for_destructive_ops` |
| 24.5 Context budget 定義 | `core/config.py`, `memory_retriever.py` | `test_context_budget_defaults` |
| 24.6 Quarantine vs correction | `core/episodic_memory.py` | `test_correction_target_quarantined` |
| 24.7 P0 測試映射 | 全部 | 見 §8.6 對照 |

### 8.3 §25（v1.2 P1）
| 條款 | 模組 | 測試 |
| --- | --- | --- |
| 25.1 sidecar | `core/working_memory.py` | `test_working_sidecar_*` |
| 25.2 Context 契約 | `core/context_builder.py` | `test_context_output_contract` |
| 25.3 Tool Registry | `core/tools.py` | `test_tool_registry.py` |
| 25.4 MockProvider 確定性 | `providers/mock_provider.py` | `test_mock_provider_modes` |
| 25.5 Benchmark schema | `benchmarks/dataset.py` | `test_benchmark_schema.py` |
| 25.6 結構化驗證產物 | `reports/` | pytest JUnit XML + coverage XML 實際存在 |
| 25.7 /health 語義 | `core/health.py` | `test_health_semantics` |

### 8.4 §26 / §28 / §29 / §30 / §33–§36
| 條款 | 模組 | 測試 |
| --- | --- | --- |
| 26.1 LTM 容量 5000 | `long_term_memory.py` | `test_long_term_capacity` |
| 26.2 Migration 原子性 | `migrations.py` | `test_migration_journal_and_recovery` |
| 26.3 flat layout | `pyproject.toml` | `test_package_layout` |
| 28.1 sidecar 交易/journal | `working_memory.py` | `test_working_sidecar_crash_recovery_matrix` |
| 28.2 maintenance lock | `migrations.py` | `migration_blocks_api_writes` |
| 28.3 restore 鎖策略 | `backup.py` | `restore_concurrency_and_crash_recovery` |
| 28.4 暫存檔命名/孤兒 | `fsio.py` | `orphan_temp_cleanup_allowlist` |
| 29.1 session 建立 API | `api/routes/sessions.py` | `test_session_create` |
| 29.2 benchmark 評分 | `benchmarks/runner.py` | `test_fact_recall_deterministic` |
| 29.3 context 模板 | `context_builder.py` | `test_context_xml_escaping` |
| 29.4 ToolSpec/RegisteredTool | `tools.py` | `test_toolspec_not_serializable_handler` |
| 29.5 總請求預算 | `budget.py`, `context_builder.py` | `test_request_budget_exceeded` |
| 29.6 quarantine acknowledge | `episodic_memory.py`, `api/routes/memory.py` | `test_quarantine_acknowledge` |
| 29.7 secret scanner 預設 | `secret_scanner.py` | `test_secret_patterns` |
| 29.8 logging redaction | `redaction.py`, `logging_config.py` | `log_redaction_removes_secret_patterns` |
| 29.9 README 一致性 | README/命令 | `phase8_command_verify.txt` |
| 30.1 metrics rotation | `metrics.py` | `test_metrics_rotation` |
| 30.2 capacity metrics | `long_term_memory.py` | `test_capacity_metrics` |
| 30.3 rate limit | `api/rate_limit.py` | `test_rate_limit` |
| 30.6 dashboard token | `dashboard/components/*` | `test_dashboard_admin_token_input` |
| 30.7 health dir | `core/health.py` | `test_health_dir_symlink` |
| 33.1 initializer | 全 package | `test_package_initializer_names` |
| 33.2 snapshot durable | `working_memory.py` | `test_old_snapshots_are_durable_before_prepared_journal` |
| 33.3 recovery lock | `recovery.py` | `test_recovery_requires_cross_process_working_lock` |
| 33.4 驗收測試 | `recovery.py` | `test_recovery_lock_timeout_fails_closed`, `test_recovery_lock_covers_journal_read_and_cleanup` |
| 34.1 journal 原子化 | `working_memory.py` | `test_prepared_journal_uses_atomic_temp_replace`, `test_journal_atomic_replace_crash_matrix` |
| 34.2 replace 失敗回滾 | `working_memory.py` | `test_replace_failure_rolls_back_under_same_lock` |
| 34.3 hash 驗證 | `working_memory.py` | `test_rollback_rejects_unknown_current_hash` |
| 34.4 migration 鎖生命週期 | `migrations.py` | `test_migration_lock_timeout_releases_all_acquired_locks`, `test_migration_failure_marker_state_machine`, `test_migration_lock_order_is_enforced` |
| 35.4 高熵 allowlist | `secret_scanner.py` | `test_entropy_allowlist` |
| 35.5 Streamlit token | dashboard | `test_dashboard_token_not_reused` |
| 35.6 metrics lock 共用 | `metrics.py` | `test_metrics_rotation_uses_resource_lock` |
| 35.7 多 worker rate limit | `rate_limit.py` | `test_multiworker_rate_limit_refuses_memory_backend` |
| 36.1 per-txn 子目錄 | `working_memory.py` | `test_working_txn_uses_per_txn_subdirectory`, `test_working_txn_snapshot_retention_counts_per_txn`, `test_working_txn_concurrent_prepare_keeps_distinct_dirs` |
| 36.2 rolled_back 恢復 | `recovery.py` | `test_rolled_back_recovery_is_idempotent`, `test_rolled_back_with_new_hash_fails_closed`, `test_rolled_back_with_external_modification_fails_closed` |
| 36.3 啟動順序 | `recovery.py`, `api/main.py` | `test_startup_runs_migration_recovery_before_session_recovery`, `test_startup_rejects_api_during_maintenance_recovery`, `test_startup_no_marker_proceeds_to_session_recovery` |
| 36.4 journal temp 孤兒 | `fsio.py`, `recovery.py` | `test_journal_temp_orphan_cleanup_allowlist`, `test_journal_temp_rejects_symlink_and_fake_names` |
| 36.5 孤兒 txn 子目錄 | `recovery.py` | `test_orphan_txn_directory_cleanup`, `test_orphan_txn_partial_commit_fails_closed`, `test_orphan_txn_respects_age_threshold` |
| 36.6 replace 後 fsync 前 crash | `working_memory.py` | crash matrix 覆蓋；Windows directory fsync 標 xfail |
| 36.7 rollback 失敗後狀態 | `working_memory.py`, `api/deps.py` | `test_rollback_failure_releases_lock_and_marks_session`, `test_recovery_required_blocks_writes_until_startup_recovery` |

### 8.5 §30.4 指定測試名稱對照（必須是真實 pytest node ID）
`backup_manifest_tamper_rejected`、`admin_token_required_for_destructive_ops`、`budget_exhaustion_stops_calls`、`compressor_preserves_required_fields`、`provider_missing_credentials_fails`、`log_redaction_removes_secret_patterns`、`docker_healthcheck_and_volume_persistence`（無 Docker → NOT VERIFIED）、`working_sidecar_crash_recovery_matrix`、`migration_blocks_api_writes`、`restore_concurrency_and_crash_recovery`、`orphan_temp_cleanup_allowlist`。

### 8.6 §24.7 強制測試
`backup_manifest_tamper_rejected`、`admin_token_*`、`budget_exhaustion_stops_calls`、`compressor_preserves_required_fields`、`provider_missing_credentials_fails`、`log_redaction_removes_secret_patterns`、`docker_*`（視環境）、`lock timeout / 鎖順序 / backup-vs-update 競爭`。

---

## 9. 已知取捨（實作前宣告）

1. **單機單使用者**：v1 以 session 目錄隔離，但無帳號體系；API 預設僅 127.0.0.1。
2. **單 worker**：跨進程鎖已實作並以多進程測試驗證，但多 worker 部署的 rate limit 需共享後端（v1 未內建 SQLite 共享計數器 → 多 worker 設定會被拒絕啟動）。
3. **directory fsync**：Windows 上不保證，記錄為已知限制。
4. **LLM 壓縮**：預設關閉（`NANOMEMORY_COMPRESSOR_LLM_ASSISTED=false`）。
5. **向量檢索**：不納入必要依賴；retriever 採關鍵字/tag/category/importance/recency。
6. **Benchmark 品質分數**：deterministic fact recall 由 runner 計算；`task_success` 需人工 rubric 標註，mock benchmark 不宣稱真實節省。

---

## 10. 交付狀態用語

- **VERIFIED**：有 `reports/` 下實際 JUnit XML / coverage XML / 命令輸出可檢查。
- **NOT IMPLEMENTED**：明確未做，且文件與最終報告列出。
- **NOT VERIFIED**：已實作但此環境無法執行驗證（例如 Docker）。
