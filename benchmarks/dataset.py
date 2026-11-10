"""Benchmark dataset schema, loading and hashing (spec §25.5, §29.2).

A case is a JSON object::

    {
      "id": "case-001",
      "task": "…",
      "history": ["user: …", "assistant: …"],
      "summary": "…",                      # pre-generated summary for the summary strategy
      "facts": ["durable fact 1", "…"],    # what a memory engine would have stored
      "required_facts": [
         {"id": "f1", "text": "…", "match_terms": ["windows 11"]}
      ],
      "forbidden_claims": ["linux only"],
      "expected_outcome": "…",
      "max_tokens": 800
    }

``match_terms`` make fact recall deterministic and reproducible (spec §29.2): matching is
case-insensitive, NFKC-normalised and substring based.  No LLM judge is used for recall.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "datasets" / "memory_bench.json"
RESULTS_DIR = PROJECT_ROOT / "benchmarks" / "results"


@dataclass(slots=True)
class RequiredFact:
    """One fact that a good answer must contain."""

    id: str
    text: str
    match_terms: list[str] = field(default_factory=list)

    def matches(self, normalized_output: str) -> tuple[bool, list[str]]:
        terms = self.match_terms or [self.text]
        hits = [_normalize(term) for term in terms if _normalize(term) in normalized_output]
        return bool(hits), hits


@dataclass(slots=True)
class BenchmarkCase:
    """One benchmark case."""

    id: str
    task: str
    required_facts: list[RequiredFact]
    history: list[str] = field(default_factory=list)
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    max_tokens: int = 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "history": self.history,
            "summary": self.summary,
            "facts": self.facts,
            "required_facts": [
                {"id": fact.id, "text": fact.text, "match_terms": fact.match_terms}
                for fact in self.required_facts
            ],
            "forbidden_claims": self.forbidden_claims,
            "expected_outcome": self.expected_outcome,
            "max_tokens": self.max_tokens,
        }


@dataclass(slots=True)
class Dataset:
    """Loaded dataset plus its integrity metadata."""

    cases: list[BenchmarkCase]
    sha256: str
    schema_version: int
    path: str
    name: str

    def case_dicts(self) -> list[dict[str, Any]]:
        return [case.to_dict() for case in self.cases]


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def load_dataset(path: Path | str | None = None) -> Dataset:
    """Load and validate a benchmark dataset; raise ``ValueError`` on schema problems."""
    target = Path(path) if path is not None else DEFAULT_DATASET
    raw_bytes = target.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dataset must be a JSON object")
    schema_version = int(payload.get("dataset_schema_version", 0))
    if schema_version != DATASET_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported dataset_schema_version {schema_version}; expected {DATASET_SCHEMA_VERSION}"
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("dataset must contain a non-empty 'cases' list")
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"case #{index} must be an object")
        case_id = str(raw.get("id") or f"case-{index:03d}")
        if case_id in seen:
            raise ValueError(f"duplicate case id {case_id}")
        seen.add(case_id)
        required = raw.get("required_facts") or []
        facts: list[RequiredFact] = []
        for fact_index, fact in enumerate(required):
            if not isinstance(fact, dict):
                raise ValueError(f"case {case_id}: required_facts entries must be objects")
            facts.append(
                RequiredFact(
                    id=str(fact.get("id") or f"f{fact_index + 1}"),
                    text=str(fact.get("text", "")),
                    match_terms=[str(term) for term in fact.get("match_terms") or []],
                )
            )
        cases.append(
            BenchmarkCase(
                id=case_id,
                task=str(raw.get("task", "")),
                required_facts=facts,
                history=[str(item) for item in raw.get("history") or []],
                summary=str(raw.get("summary", "")),
                facts=[str(item) for item in raw.get("facts") or []],
                forbidden_claims=[str(item) for item in raw.get("forbidden_claims") or []],
                expected_outcome=str(raw.get("expected_outcome", "")),
                max_tokens=int(raw.get("max_tokens", 1000)),
            )
        )
    return Dataset(
        cases=cases,
        sha256=digest,
        schema_version=schema_version,
        path=str(target.resolve()),
        name=str(payload.get("name", target.stem)),
    )


def normalize_output(text: str) -> str:
    """Normalisation used by deterministic fact matching."""
    return _normalize(text)


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DATASET",
    "RESULTS_DIR",
    "BenchmarkCase",
    "Dataset",
    "RequiredFact",
    "RESULT_SCHEMA_VERSION",
    "load_dataset",
    "normalize_output",
]
