"""Explainable hybrid ranking (dense + BM25 + recency + importance).

Dense retrieval catches paraphrase; BM25 catches exact identifiers, product codes and names
that embeddings blur.  Both signals are computed over the *same* pre-filtered candidate set,
so the hybrid step cannot widen access.

Two numeric choices are deliberate:

* BM25 is min-max normalised *within the candidate set* so the best lexical match always
  scores 1.0; raw BM25 depends on corpus size and cannot be thresholded directly.
* Refusal uses an absolute signal instead: dense cosine or IDF-weighted query coverage.
  Coverage answers "did the corpus match the distinctive words at all?" without inheriting
  BM25's corpus-dependent scale, which is what makes the answer/refuse decision measurable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nanomemory.core.memory_retriever import QueryIntent, detect_intent
from nanomemory.vector.schemas import MemoryMetadata, VectorMemoryRecord

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")

_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "that", "this", "from", "into", "about", "have", "was", "you"}
)

BM25_K1 = 1.5
BM25_B = 0.75

# dense, sparse, recency, importance
INTENT_WEIGHTS: dict[QueryIntent, tuple[float, float, float, float]] = {
    QueryIntent.PREFERENCE: (0.65, 0.20, 0.05, 0.10),
    QueryIntent.PROJECT_STATE: (0.45, 0.35, 0.15, 0.05),
    QueryIntent.DECISION_HISTORY: (0.55, 0.30, 0.10, 0.05),
    QueryIntent.TIMELINE: (0.40, 0.25, 0.30, 0.05),
    QueryIntent.GENERAL: (0.55, 0.30, 0.10, 0.05),
}


def tokenize(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {token for token in _WORD.findall(lowered) if token not in _STOPWORDS}
    for phrase in _CJK.findall(lowered):
        for index in range(len(phrase) - 1):
            tokens.add(phrase[index : index + 2])
    return tokens


def bm25(query: set[str], documents: list[set[str]]) -> list[float]:
    if not documents:
        return []
    total = len(documents)
    average = sum(len(document) for document in documents) / total or 1.0
    frequency = Counter(token for document in documents for token in document)
    scores: list[float] = []
    for document in documents:
        counts = Counter(document)
        length = len(document) or 1
        score = 0.0
        for token in query:
            tf = counts.get(token, 0)
            if not tf:
                continue
            df = frequency[token]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += (
                idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
            )
        scores.append(score)
    return scores


def coverage(query: set[str], documents: list[set[str]]) -> list[float]:
    """IDF-weighted share of the query's distinctive tokens present in each document."""
    if not documents or not query:
        return [0.0] * len(documents)
    total = len(documents)
    frequency = Counter(token for document in documents for token in document)
    weights = {
        token: math.log(
            1 + (total - frequency.get(token, 0) + 0.5) / (frequency.get(token, 0) + 0.5)
        )
        for token in query
    }
    denominator = sum(weights.values()) or 1.0
    return [
        sum(weight for token, weight in weights.items() if token in document) / denominator
        for document in documents
    ]


def minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 if high > 0 else 0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def recency(stamp: str | None, now: datetime, *, half_life_days: float = 120.0) -> float:
    if not stamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_days = max(0.0, (now - parsed).total_seconds() / 86400)
    return math.exp(-age_days / half_life_days)


def importance(metadata: MemoryMetadata) -> float:
    base = (metadata.importance - 1) / 4.0
    if metadata.confidence.value == "high":
        base = min(1.0, base + 0.1)
    return base


@dataclass(slots=True)
class ScoredCandidate:
    record: VectorMemoryRecord
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def rank_candidates(
    records: list[VectorMemoryRecord],
    dense_scores: list[float],
    query: str,
    *,
    now: datetime | None = None,
    intent: QueryIntent | None = None,
) -> list[ScoredCandidate]:
    """Blend both signals; inputs are already scoped to one tenant/user."""
    if not records:
        return []
    moment = now or datetime.now(UTC)
    selected_intent = intent or detect_intent(query)
    query_tokens = tokenize(query)
    documents = [tokenize(record.content) for record in records]
    sparse = minmax(bm25(query_tokens, documents))
    cover = coverage(query_tokens, documents)
    dense = [max(0.0, min(1.0, value)) for value in dense_scores]
    weights = INTENT_WEIGHTS[selected_intent]

    scored: list[ScoredCandidate] = []
    for index, record in enumerate(records):
        recall = recency(record.updated_at or record.created_at, moment)
        weight = importance(record.metadata)
        score = (
            weights[0] * dense[index]
            + weights[1] * sparse[index]
            + weights[2] * recall
            + weights[3] * weight
        )
        signals = {
            "dense": round(dense[index], 4),
            "sparse": round(sparse[index], 4),
            "lexical_coverage": round(cover[index], 4),
            "recency": round(recall, 4),
            "importance": round(weight, 4),
        }
        reasons = [f"{name} {value:.2f}" for name, value in signals.items() if value >= 0.25]
        scored.append(
            ScoredCandidate(
                record=record,
                score=max(0.0, min(1.0, round(score, 6))),
                signals=signals,
                reason=", ".join(reasons) or "weak match",
            )
        )
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def content_signal(scored: list[ScoredCandidate]) -> float:
    """Best absolute evidence that the corpus contains an answer (not recency/importance)."""
    best = 0.0
    for candidate in scored:
        best = max(
            best,
            candidate.signals.get("dense", 0.0),
            candidate.signals.get("lexical_coverage", 0.0),
        )
    return best


__all__ = [
    "BM25_B",
    "BM25_K1",
    "INTENT_WEIGHTS",
    "ScoredCandidate",
    "bm25",
    "content_signal",
    "coverage",
    "importance",
    "minmax",
    "rank_candidates",
    "recency",
    "tokenize",
]
