"""Diagnose the 0.39 fact-recall result: retrieval failure vs vocabulary gap.

For every required fact it checks whether the fact's match terms appear in the context the
retriever actually selected, and whether the task shares any keyword with the stored fact.
That separates three causes:

* ``present``            - the term was in the selected context (answer/scoring issue, not retrieval),
* ``absent_outranked``   - the fact is stored, shares keywords with the task, but was not selected
                           (ranking / item cap / budget),
* ``absent_lexical_gap`` - the fact is stored but shares no keyword with the task
                           (needs embeddings / entity matching / query rewriting).

Usage::

    python reports/analyse_recall.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.dataset import load_dataset, normalize_output  # noqa: E402
from benchmarks.runner import STRATEGIES, BenchmarkRunner, ContextEchoProvider  # noqa: E402
from nanomemory.core.config import Settings  # noqa: E402

WORD = re.compile(r"[A-Za-z0-9_]{2,}")


def tokens(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower())}


def main() -> int:
    dataset = load_dataset(PROJECT_ROOT / "benchmarks" / "datasets" / "memory_bench_long.json")
    settings = Settings(
        storage_root=PROJECT_ROOT / ".recall-analysis" / "storage",
        environment="test",
        log_level="WARNING",
        metrics_enabled=False,
    )
    runner = BenchmarkRunner(settings, provider=ContextEchoProvider())
    working, long_term, episodic, retriever = runner._build_memory_stack(
        PROJECT_ROOT / ".recall-analysis"
    )
    session_id = working.settings.default_session_id
    working.ensure_session(session_id)
    for case in dataset.cases:
        working.update(
            session_id,
            __import__("nanomemory.core.memory_models", fromlist=["MemoryUpdateProposal"])
            .MemoryUpdateProposal(current_goal=case.task, allow_task_removal=True),
            expected_version=working.read(session_id).meta.memory_version,
        )
        for index, fact in enumerate(case.facts):
            long_term.add(
                {
                    "id": f"{case.id}-fact-{index:02d}",
                    "content": fact,
                    "category": "benchmark",
                    "tags": [case.id],
                    "source": f"benchmark:{case.id}",
                    "source_type": "import",
                    "importance": 4,
                }
            )

    counts = {"present": 0, "absent_outranked": 0, "absent_lexical_gap": 0}
    details: dict[str, list[dict[str, object]]] = {key: [] for key in counts}
    total = 0
    for case in dataset.cases:
        retrieval = retriever.retrieve(case.task, session_id=session_id)
        selected_text = normalize_output("\n".join(item.content for item in retrieval.selected))
        task_tokens = tokens(case.task)
        for fact in case.required_facts:
            total += 1
            terms = [normalize_output(term) for term in (fact.match_terms or [fact.text])]
            present = any(term in selected_text for term in terms)
            if present:
                counts["present"] += 1
                details["present"].append({"case": case.id, "fact": fact.id})
                continue
            stored = next(
                (
                    text
                    for text in case.facts
                    if any(term in normalize_output(text) for term in terms)
                ),
                "",
            )
            overlap = sorted(task_tokens & tokens(stored))
            kind = "absent_outranked" if overlap else "absent_lexical_gap"
            counts[kind] += 1
            details[kind].append(
                {
                    "case": case.id,
                    "fact": fact.id,
                    "terms": fact.match_terms,
                    "stored_fact": stored[:90],
                    "task_overlap": overlap[:5],
                }
            )

    print(json.dumps({"total_required_facts": total, "counts": counts}, indent=2))
    print(f"context recall (term present): {counts['present'] / total:.3f}")
    for kind in ("absent_lexical_gap", "absent_outranked"):
        print(f"\n{kind}: {counts[kind]}")
        for item in details[kind][:10]:
            print("  ", json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
