# NanoMemory 2.0 — product and architecture specification

This document records the 2.0 direction requested in the product review, and the mapping to
code. It is the durable version of that review: the screenshot is not a specification, this
file is.

> **Tagline:** *Your AI. Your Memory.* — persistent memory for any AI application.
> **Positioning:** a *universal AI memory engine* — independent of model and host, able to run
> continuously, be migrated, and be verified. Not a Markdown manager, not a vector-DB wrapper,
> not a RAG plugin, not a DeepSeek-only plugin.

## 1. Five principles

1. Memory belongs to the user, not the model.
2. Every memory has provenance.
3. Memory updates are traceable and reversible.
4. Retrieval quality matters more than memory quantity.
5. The system works without requiring users to understand its internals.

## 2. Seven layers

| Layer | Responsibility | Code |
| --- | --- | --- |
| L7 Memory Interface | SDK / REST / MCP / CLI / Dashboard | `nanomemory/sdk.py`, `api/`, `dsh-plugin/`, `cli.py`, `dashboard/` |
| L6 Cognitive Memory Controller | memory decisions, lifecycle, retrieval orchestration | `core/cognitive_controller.py` |
| L5 Retrieval | hybrid retrieval, associative reasoning | `core/memory_retriever.py` (v2 scoring) |
| L4 Consolidation | dedupe, merge, compress | `core/memory_formation.py`, `core/memory_compressor.py` |
| L3 Memory Formation | extraction, classification, importance | `core/memory_formation.py` |
| L2 Memory Model | facts, events, relations, time | `core/memory_models.py` |
| L1 Storage | persistence, index, backup | `core/working_memory.py`, `core/long_term_memory.py`, `core/episodic_memory.py` |
| L0 Foundation | transactions, safety, recovery, validation | `core/fsio.py`, `core/locks.py`, `core/recovery.py`, `core/secret_scanner.py` |

## 3. Structured memory model

Every long-term memory carries:

`memory_id, content, memory_type, source, confidence, importance, created_at, updated_at,
valid_from, valid_until, related_ids, status, provenance`

`memory_type` ∈ `fact | preference | event | procedure | relationship | autobiographical`.
Working Memory, short-term memory and episodic events keep their own stores; the type field
stops a single Markdown file from carrying every kind of state.

`provenance` is a nested object:

```json
{"source_kind": "user|model|tool|file|composed|import",
 "source_ref": "conversation:… / path / url / memory id",
 "derived_from": ["mem-…"],
 "created_by": "sdk|api|formation|migration",
 "note": "why this memory exists"}
```

A model inference is never stored as a user-stated fact: `source_kind=model` memories default
to `PENDING_CONFIRMATION` unless an explicit confirmation path is used.

`status` ∈ `ACTIVE | SUPERSEDED | CONFLICT | ARCHIVED | PENDING_CONFIRMATION`.
Updates never delete evidence: the old record becomes `SUPERSEDED` with `superseded_by`, and
`valid_until` is set at supersede time.

## 4. Automatic memory formation

```
Conversation/Input → Information Extraction → Candidate Memories → Validation
   → Existing memory? ─No→ Create ─┐
                     └Yes→ Conflict/Duplicate analysis → Merge/Update/Preserve
   → Persistence → Audit Event → Retrieval Index
```

* extraction is deterministic by default (rules + structured proposals); an LLM may propose
  candidates but the deterministic validator decides what is written,
* per-type policy: user-stated preferences/facts may be `ACTIVE` directly; model inferences
  become `PENDING_CONFIRMATION`; duplicates merge; contradictions mark `CONFLICT` and keep
  both records,
* every commit writes an episodic audit event.

## 5. Hybrid retrieval

`S(m,q) = w1·S_semantic + w2·S_keyword + w3·S_relation + w4·S_recency + w5·S_importance`

* keyword: BM25 over content/category/tags,
* semantic-lite: character-trigram cosine (no external embedding service required; embeddings
  can be added behind the same interface in Advanced mode),
* metadata filters: `memory_type`, `user_id`, category, status, validity window,
* graph: one-hop expansion through `related_ids` / supersede chains,
* query intent: `preference | project_state | decision_history | timeline | general` selects
  weights,
* reranking: intent-aware boosts, conflict penalty, low-confidence fallback
  (`insufficient_context=true` instead of pretending to answer).

## 6. Lifecycle and explainability

Retention score = `importance × utility × recency × confidence` (normalised, documented). Low
retrieval frequency alone never deletes a memory: stable preferences are protected, and the
action is always `ARCHIVED` (reversible), never a hard delete.

`explain(memory_id)` returns: the record, its provenance, the supersede chain, related ids and
the audit events that created/updated/superseded it. A retrieval result explains itself with
per-signal scores and a reason string.

## 7. Integration surface

| Interface | Status in 0.2.0 |
| --- | --- |
| Python SDK (`from nanomemory import Memory`) | implemented |
| REST API | implemented |
| MCP server (`mcp__nanomemory__*`) | implemented |
| CLI (`nanomemory init/start/status/remember/recall/explain/doctor`) | implemented |
| Dashboard | implemented |
| Storage adapter (files now, SQLite later) | interface documented, files implemented |

Runtime modes: **Lite** (deterministic only, no provider, no key), **Standard** (provider
optional, formation + retrieval v2), **Advanced** (multi-user/SQLite/distributed — not in
0.2.0).

## 8. Roadmap status

| Phase | Content | Status |
| --- | --- | --- |
| P0 Memory Quality | BM25 + semantic-lite hybrid, reranking, relation expansion, recall benchmark, fallback | implemented (embeddings optional, not bundled) |
| P1 Cognitive Memory | formation, classification, dedupe, conflict, consolidation, lifecycle | implemented deterministically; LLM-assisted extraction is opt-in and off |
| P2 Product Experience | pip install, CLI init/start, SDK, MCP generalisation, backup | implemented except PyPI publication |
| P3 Advanced Memory | relation graph, timeline reasoning, cross-project, user profile, self-evaluation | partial: relations + timeline fields; no profile model |
| P4 Production | multi-user isolation, permissions, SQLite, multi-instance, observability | not implemented (Local single-user MVP) |

## 9. Acceptance metrics

`Fact Recall`, `Precision@K`, `Conflict Resolution`, `Memory Update Accuracy`,
`Temporal Accuracy`, `Provenance Coverage`, `Token Efficiency`, `Data Recovery`.

Token savings without recall are not success: the benchmark reports recall and token use side
by side, and the README quotes both.
