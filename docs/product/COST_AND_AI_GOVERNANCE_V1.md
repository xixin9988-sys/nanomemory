# NanoMemory Vector Memory v1 — Cost, Legal and AI Governance Plan

Status: **proposal; not a certification, legal opinion or measured cost report**  
Date: 2026-11-27

## 1. Unit economics

Do not choose a price or promise a gross margin until a real deployment has produced measured provider, database, compute and support costs. Record exact provider usage where supplied; mark estimates separately.

### Cost components

For a billing period:

- Embedding ingestion cost = total billable input tokens sent to embedding providers × provider price per token unit.
- Query embedding cost = recall queries embedded × query embedding price.
- Optional reranking cost = reranker input tokens or requests × reranker price.
- Database cost = allocated PostgreSQL compute + storage + IOPS + backups + monitoring.
- Storage cost = canonical content + metadata + embedding payload + ANN index overhead + WAL/backup growth.
- Operations cost = logs/traces, queue workers, network transfer and support allocation.
- Local embedding cost = allocated compute/GPU hours + host memory and electricity where applicable.

For a record with d dimensions:
- float32 vector payload is approximately 4 × d bytes before row/TOAST/index overhead;
- half precision payload is approximately 2 × d bytes before row/index overhead.
Measure actual relation and index sizes in PostgreSQL; the formula is not a billable storage estimate.

### Unit metrics to persist

- cost per 1,000 memories ingested;
- cost per 1,000 recall requests;
- embedding tokens per memory and per query;
- average and P95 vector query latency;
- ANN index bytes per active memory;
- active memories and storage bytes per tenant;
- failed/retried embedding jobs and re-indexing backlog;
- optional reranker and LLM cost separately from NanoMemory core cost;
- gross contribution per tenant and support cost per tenant.

Every usage record includes provider, model, profile version, currency, quantity, unit, exact/estimated source, timestamp, tenant and request ID. Never merge exact usage with estimates without retaining the distinction.

### Quotas and spend controls

- Per-tenant request, token, ingestion and storage quotas.
- Per-principal rate limits and maximum batch sizes.
- Global provider spend budget and per-tenant spend cap.
- Bounded concurrent embedding jobs, timeouts and retries.
- Backpressure and a visible pending-index queue.
- Explicit over-quota response with retry/reset information; never silently drop memory writes.
- Alert thresholds for spend anomaly, retry storm, index backlog and P95 latency.

### Commercial metric

Monthly contribution per tenant:
Revenue per tenant − embedding cost − database/storage/backup allocation − reranking cost − traffic/observability allocation − support allocation.

Track new-tenant setup cost and migration cost separately from recurring unit economics. Pricing tiers should be based on measured quota drivers, not simply "number of memories".

## 2. Legal and IP release checklist

This is an engineering inventory, not legal advice. Before a commercial launch:
- Confirm the repository's top-level license and preserve copyright / notice requirements.
- Generate a Software Bill of Materials (SBOM) for every release.
- Scan direct and transitive dependency licenses and known vulnerabilities.
- Record each embedding model/provider's terms, data retention, training use, geographic processing, rate limits and commercial-use permissions.
- Record third-party datasets and evaluation-set provenance; do not copy private customer conversations into public fixtures.
- Identify trademarks, project names, logos, generated assets and any externally sourced code.
- Document ownership and contribution terms for outside contributors.
- Document patent notices, third-party patent terms and a process for receiving IP claims.
- Define customer data ownership, controller/processor responsibilities, deletion/export, backup retention, subprocessors and incident notification.
- Maintain a legal/IP FAQ explaining the license, third-party dependencies, provider/model terms, data flow and known restrictions.

Do not claim that the MIT license grants rights to the embedding model, provider service, evaluation datasets or third-party components. Each has its own terms.

## 3. AI risk governance mapping

Use NIST AI RMF as a voluntary product-risk management structure and consider ISO/IEC 42001 as an organizational AI management-system framework. Mapping controls to a framework is not certification or proof of compliance. NIST currently states that AI RMF 1.0 is being revised; track the current version and profiles before claiming alignment.

| Governance theme | NanoMemory evidence to maintain |
|---|---|
| Govern | Named product/security owner, scope/non-goals, policy approval, risk acceptance, supplier/provider register, incident and change process |
| Map | Intended use, excluded use, data-flow diagram, threat model, affected users, tenant boundaries, embedding provider dependencies, misuse scenarios |
| Measure | 50+ labelled retrieval cases, abstention/grounding, cross-tenant attack tests, embedding quality, latency/cost, drift and incident metrics |
| Manage | Risk treatment plan, fail-closed policy, release gates, quota controls, backup/restore, rollback, incident response, retention and continual review |

### Required governance artifacts

- Product scope brief and recorded owner approval.
- Architecture decision records and versioned API/data contracts.
- Threat model and risk register with owner, severity, mitigation and residual risk.
- Model/provider inventory including model version, dimensions, license, region and data handling terms.
- Dataset register including source, consent/rights, labels, retention and hash.
- Evaluation report with versioned dataset, per-category scores, failure examples and accepted residual risks.
- Human escalation path for disputed, sensitive or conflicting memories.
- Incident log and post-incident corrective actions.
- Release review showing which risks were tested, accepted, deferred or blocked.

## 4. Review cadence

- Every release: dependency/SBOM and secret scans, security tests, migration compatibility, retrieval eval and release-gate record.
- Weekly during pilot: review user feedback, false recalls, abstentions, permission denials, cost and latency.
- Monthly: review provider pricing/terms, quota settings, storage/index growth, backups and open risks.
- On any embedding-model change: run a shadow backfill, compare recall and cost, then switch only after explicit approval.
- On any data boundary or authorization change: repeat cross-tenant and cross-user adversarial tests before release.

## 5. Approval

- Product owner: PENDING
- Technical owner: PENDING
- Security / privacy owner: PENDING
- Commercial / legal reviewer: PENDING
- Date: PENDING
