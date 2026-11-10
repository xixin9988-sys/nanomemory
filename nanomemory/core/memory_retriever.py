"""Hybrid memory retrieval (v2) with explainable scores and a hard token budget.

Scoring follows the 2.0 spec::

    S(m, q) = w1·S_semantic + w2·S_keyword + w3·S_relation + w4·S_recency + w5·S_importance

* **keyword** — BM25 over content, category and tags (no external service),
* **semantic** — character-trigram cosine, a dependency-free semantic-lite signal; a real
  embedding provider can replace it behind the same interface,
* **relation** — one-hop expansion through ``related_ids`` / supersede links,
* **recency** — exponential decay of the last update,
* **importance** — the 1-5 importance plus a small confidence term.

The weights depend on the detected query intent (preference, project state, decision
history, timeline, general). When even the best long-term candidate stays below the
relevance floor the result says ``insufficient_context`` instead of pretending to answer.

Every selected item carries its per-signal scores so the caller can explain *why* it was
retrieved.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.episodic_memory import EpisodicMemoryStore, EpisodicQuery
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import (
    LongTermRecord,
    MemoryStatus,
    MemoryType,
    RetrievalResult,
    RetrievedMemory,
    TrustLevel,
)
from nanomemory.core.token_counter import TokenCounter
from nanomemory.core.working_memory import WorkingMemoryStore

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
MIN_FINAL_SCORE = 0.18
MIN_CONTENT_SCORE = 0.22
MIN_EPISODIC_SCORE = 0.2
MAX_LONG_TERM_ITEMS = 8
MAX_EPISODIC_ITEMS = 5
BM25_K1 = 1.5
BM25_B = 0.75

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "about",
        "have",
        "will",
        "your",
        "you",
        "our",
        "are",
        "was",
        "were",
        "task",
        "please",
        "使用",
        "請",
        "這個",
    }
)


class QueryIntent(StrEnum):
    """What kind of memory the question is asking for."""

    PREFERENCE = "preference"
    PROJECT_STATE = "project_state"
    DECISION_HISTORY = "decision_history"
    TIMELINE = "timeline"
    GENERAL = "general"


_INTENT_HINTS: dict[QueryIntent, tuple[str, ...]] = {
    QueryIntent.PREFERENCE: (
        "prefer",
        "preference",
        "favorite",
        "favourite",
        "like",
        "style",
        "convention",
        "習慣",
        "偏好",
        "喜歡",
        "风格",
        "風格",
        "习惯",
    ),
    QueryIntent.PROJECT_STATE: (
        "project",
        "status",
        "progress",
        "currently",
        "working on",
        "todo",
        "next step",
        "項目",
        "项目",
        "進度",
        "进度",
        "做到",
        "目前",
        "下一步",
    ),
    QueryIntent.DECISION_HISTORY: (
        "why",
        "decided",
        "decision",
        "chose",
        "choice",
        "reason",
        "為什麼",
        "为什么",
        "決定",
        "决定",
        "選擇",
        "选择",
        "原因",
    ),
    QueryIntent.TIMELINE: (
        "when",
        "timeline",
        "history",
        "last time",
        "earlier",
        "before",
        "什麼時候",
        "什么时候",
        "時間線",
        "时间线",
        "之前",
        "上次",
    ),
}

# w_semantic, w_keyword, w_relation, w_recency, w_importance
_INTENT_WEIGHTS: dict[QueryIntent, tuple[float, float, float, float, float]] = {
    QueryIntent.PREFERENCE: (0.35, 0.25, 0.15, 0.05, 0.20),
    QueryIntent.PROJECT_STATE: (0.25, 0.35, 0.15, 0.20, 0.05),
    QueryIntent.DECISION_HISTORY: (0.20, 0.30, 0.20, 0.15, 0.15),
    QueryIntent.TIMELINE: (0.15, 0.25, 0.10, 0.40, 0.10),
    QueryIntent.GENERAL: (0.30, 0.30, 0.15, 0.15, 0.10),
}

_INTENT_TYPE_BOOST: dict[QueryIntent, dict[MemoryType, float]] = {
    QueryIntent.PREFERENCE: {MemoryType.PREFERENCE: 0.20, MemoryType.AUTOBIOGRAPHICAL: 0.05},
    QueryIntent.PROJECT_STATE: {
        MemoryType.FACT: 0.06,
        MemoryType.PROCEDURE: 0.10,
        MemoryType.EVENT: 0.04,
    },
    QueryIntent.DECISION_HISTORY: {
        MemoryType.EVENT: 0.12,
        MemoryType.AUTOBIOGRAPHICAL: 0.08,
    },
    QueryIntent.TIMELINE: {MemoryType.EVENT: 0.12},
    QueryIntent.GENERAL: {},
}

_STATUS_PENALTY = {
    MemoryStatus.CONFLICT: 0.15,
    MemoryStatus.NEEDS_REVIEW: 0.05,
}

_ACTIVE_ONLY = {
    MemoryStatus.ACTIVE,
    MemoryStatus.CONFLICT,
    MemoryStatus.NEEDS_REVIEW,
}


def _keywords(text: str) -> set[str]:
    """Lower-cased keyword set used for BM25 (latin words + CJK bigrams)."""
    lowered = text.lower()
    tokens = {token for token in _WORD.findall(lowered) if token not in _STOPWORDS}
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        for index in range(len(phrase) - 1):
            tokens.add(phrase[index : index + 2])
    return tokens


def _trigrams(text: str) -> set[str]:
    """Character trigrams (language independent, dependency free)."""
    squeezed = re.sub(r"\s+", " ", text.lower()).strip()
    if len(squeezed) < 3:
        return {squeezed} if squeezed else set()
    return {squeezed[index : index + 3] for index in range(len(squeezed) - 2)}


def _cosine(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _normalise(values: list[float]) -> list[float]:
    """Min-max normalise; an all-equal column collapses to a neutral 0.5."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.5 if high > 0 else 0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _bm25(query: set[str], documents: list[set[str]]) -> list[float]:
    if not documents:
        return []
    total = len(documents)
    average = sum(len(doc) for doc in documents) / total or 1.0
    document_frequency = Counter(token for doc in documents for token in doc)
    scores: list[float] = []
    for doc in documents:
        counts = Counter(doc)
        length = len(doc) or 1
        score = 0.0
        for token in query:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            df = document_frequency[token]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            score += (
                idf
                * frequency
                * (BM25_K1 + 1)
                / (frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
            )
        scores.append(score)
    return scores


def detect_intent(query: str) -> QueryIntent:
    """Classify the question with cheap, explainable hints."""
    lowered = query.lower()
    for intent in (
        QueryIntent.PREFERENCE,
        QueryIntent.PROJECT_STATE,
        QueryIntent.DECISION_HISTORY,
        QueryIntent.TIMELINE,
    ):
        for hint in _INTENT_HINTS[intent]:
            if hint in lowered:
                return intent
    return QueryIntent.GENERAL


def _age_days(stamp: str | None, now: datetime) -> float:
    if not stamp:
        return 365.0
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed).total_seconds() / 86400)


def _recency(stamp: str | None, now: datetime, *, half_life_days: float = 120.0) -> float:
    return math.exp(-_age_days(stamp, now) / half_life_days)


def rank_records(
    records: list[LongTermRecord],
    query: str,
    *,
    intent: QueryIntent | None = None,
    now: datetime | None = None,
) -> list[tuple[LongTermRecord, float, dict[str, float]]]:
    """Score records for a query; usable by retrieval and by the benchmark."""
    if not records:
        return []
    moment = now or datetime.now(UTC)
    selected_intent = intent or detect_intent(query)
    query_tokens = _keywords(query)
    query_trigrams = _trigrams(query)
    documents = [
        _keywords(f"{record.content} {record.category} {' '.join(record.tags)}")
        for record in records
    ]
    keyword_scores = _normalise(_bm25(query_tokens, documents))
    semantic_scores = _normalise(
        [_cosine(query_trigrams, _trigrams(record.content)) for record in records]
    )
    recency_scores = [
        _recency(record.updated_at or record.created_at, moment) for record in records
    ]
    importance_scores = [
        (record.importance - 1) / 4.0 + (0.1 if record.confidence.value == "high" else 0.0)
        for record in records
    ]

    base = [
        0.6 * keyword_scores[index] + 0.4 * semantic_scores[index] for index in range(len(records))
    ]
    relation_scores = _relation_scores(records, base, selected_intent)
    semantic_scores = _intent_blend(selected_intent, semantic_scores)
    keyword_scores = _intent_blend(selected_intent, keyword_scores)

    weights = _INTENT_WEIGHTS[selected_intent]
    ranked: list[tuple[LongTermRecord, float, dict[str, float]]] = []
    for index, record in enumerate(records):
        score = (
            weights[0] * semantic_scores[index]
            + weights[1] * keyword_scores[index]
            + weights[2] * relation_scores[index]
            + weights[3] * recency_scores[index]
            + weights[4] * min(1.0, importance_scores[index])
        )
        score += _INTENT_TYPE_BOOST[selected_intent].get(record.memory_type, 0.0)
        score -= _STATUS_PENALTY.get(record.status, 0.0)
        signals = {
            "semantic": round(semantic_scores[index], 4),
            "keyword": round(keyword_scores[index], 4),
            "relation": round(relation_scores[index], 4),
            "recency": round(recency_scores[index], 4),
            "importance": round(min(1.0, importance_scores[index]), 4),
        }
        ranked.append((record, max(0.0, min(1.0, score)), signals))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def _intent_blend(intent: QueryIntent, values: list[float]) -> list[float]:
    """Preference questions lean semantic; project questions lean keyword."""
    if intent is QueryIntent.PREFERENCE:
        return [min(1.0, value * 1.1) for value in values]
    if intent is QueryIntent.PROJECT_STATE:
        return [min(1.0, value * 1.1) for value in values]
    return values


def _relation_scores(
    records: list[LongTermRecord], base: list[float], intent: QueryIntent
) -> list[float]:
    """One-hop expansion: linked memories inherit a fraction of their parent's score."""
    index_by_id = {record.id: index for index, record in enumerate(records)}
    scores = [0.0] * len(records)
    for index, record in enumerate(records):
        parents: list[str] = list(record.related_ids) + list(record.supersedes)
        if record.superseded_by:
            parents.append(record.superseded_by)
        parent_scores = [base[index_by_id[parent]] for parent in parents if parent in index_by_id]
        if parent_scores:
            scores[index] = max(parent_scores) * 0.6
    if intent is QueryIntent.DECISION_HISTORY:
        scores = [min(1.0, value * 1.2) for value in scores]
    return scores


@dataclass(slots=True)
class _Candidate:
    memory: RetrievedMemory
    score: float
    reason_bits: list[str] = field(default_factory=list)


class MemoryRetriever:
    """Selects the smallest set of memories that explains the current task."""

    def __init__(
        self,
        settings: Settings,
        working: WorkingMemoryStore,
        long_term: LongTermMemoryStore,
        episodic: EpisodicMemoryStore,
        *,
        counter: TokenCounter | None = None,
    ) -> None:
        self.settings = settings
        self.working = working
        self.long_term = long_term
        self.episodic = episodic
        self.counter = counter or TokenCounter(settings)

    # ------------------------------------------------------------------ public
    def retrieve(
        self,
        task: str,
        *,
        session_id: str,
        token_budget: int | None = None,
        include_working_memory: bool = True,
        include_long_term_memory: bool = True,
        include_recent_episodes: bool = True,
        user_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> RetrievalResult:
        budget = token_budget if token_budget is not None else self.settings.memory_token_limit
        budget = max(1, int(budget))
        intent = detect_intent(task)
        selected: list[RetrievedMemory] = []
        omitted: list[dict[str, Any]] = []
        used = 0
        result = RetrievalResult(
            current_task=task,
            session_id=session_id,
            token_budget=budget,
            retrieval_reason="",
            intent=intent.value,
        )

        # Working Memory is always included first: it holds the goal, blockers and next
        # action, which must never be dropped for budget reasons (spec §22.12).
        if include_working_memory:
            working_item = self._working_item(session_id)
            tokens = self.counter.estimate(working_item.content)
            if used + tokens <= budget:
                working_item.tokens = tokens
                working_item.reason = "always included: current goal, blockers and next action"
                selected.append(working_item)
                used += tokens
            else:
                omitted.append(
                    {
                        "kind": "working",
                        "id": working_item.id,
                        "reason": "budget_exceeded_for_working_memory",
                        "tokens": tokens,
                    }
                )
                result.truncated_for_budget = True

        candidates: list[_Candidate] = []
        best_long_term = 0.0
        if include_long_term_memory:
            long_term_candidates, best_long_term = self._long_term_candidates(
                task, intent, user_id=user_id, memory_type=memory_type
            )
            candidates.extend(long_term_candidates)
        best_episodic = 0.0
        if include_recent_episodes:
            episodic_candidates, best_episodic = self._episodic_candidates(task, session_id)
            candidates.extend(episodic_candidates)

        candidates.sort(key=lambda item: item.score, reverse=True)
        long_term_items = [item for item in candidates if item.memory.kind == "long_term"][
            :MAX_LONG_TERM_ITEMS
        ]
        episodic_items = [item for item in candidates if item.memory.kind == "episodic"][
            :MAX_EPISODIC_ITEMS
        ]
        for candidate in sorted(
            long_term_items + episodic_items, key=lambda item: item.score, reverse=True
        ):
            tokens = self.counter.estimate(candidate.memory.content)
            if tokens <= 0:
                continue
            if used + tokens > budget:
                result.truncated_for_budget = True
                omitted.append(
                    {
                        "kind": candidate.memory.kind,
                        "id": candidate.memory.id,
                        "reason": "budget_exceeded",
                        "score": round(candidate.score, 4),
                        "tokens": tokens,
                    }
                )
                continue
            candidate.memory.tokens = tokens
            candidate.memory.reason = ", ".join(candidate.reason_bits) or "matched"
            selected.append(candidate.memory)
            used += tokens

        best_memory_score = max(best_long_term, best_episodic)
        content_best = 0.0
        for candidate in candidates:
            signals = candidate.memory.metadata.get("signals") or {}
            if candidate.memory.kind == "long_term":
                # Either signal alone may be the honest reason a memory is relevant.
                content_best = max(
                    content_best,
                    float(signals.get("keyword", 0.0)),
                    float(signals.get("semantic", 0.0)),
                )
            else:
                content_best = max(content_best, float(signals.get("keyword", 0.0)))
        if include_long_term_memory or include_recent_episodes:
            # The relevance floor is about content match, not about recency/importance:
            # a fresh but unrelated memory must not look like an answer.
            result.insufficient_context = content_best < MIN_CONTENT_SCORE
            if result.insufficient_context:
                result.fallback_reason = (
                    f"no memory matched the query above the content floor "
                    f"({MIN_CONTENT_SCORE:.2f}); best content signal was {content_best:.3f}"
                )

        result.selected = selected
        result.omitted = omitted
        result.estimated_tokens = used
        result.retrieval_reason = (
            f"intent {intent.value}; {len(selected)} memories selected; "
            f"{len(omitted)} omitted; budget {budget} tokens; "
            f"floor {MIN_FINAL_SCORE:.2f}; best {best_memory_score:.3f}"
        )
        return result

    # ------------------------------------------------------------------ sources
    def _working_item(self, session_id: str) -> RetrievedMemory:
        snapshot = self.working.read(session_id)
        document = snapshot.document
        lines = [
            f"Current goal: {document.current_goal}",
            f"Status: {document.status.value}",
        ]
        if document.in_progress:
            lines.append("In progress: " + "; ".join(document.in_progress))
        if document.blockers:
            lines.append("Blockers: " + "; ".join(document.blockers))
        lines.append(f"Next action: {document.next_action}")
        if document.completed:
            lines.append("Completed: " + "; ".join(document.completed[-5:]))
        return RetrievedMemory(
            kind="working",
            id=f"working:{session_id}",
            content="\n".join(lines),
            source="working_memory",
            trust_level=TrustLevel.USER_PROVIDED,
            metadata={"memory_version": snapshot.meta.memory_version},
        )

    def _long_term_candidates(
        self,
        task: str,
        intent: QueryIntent,
        *,
        user_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> tuple[list[_Candidate], float]:
        records = [
            record
            for record in self.long_term.list_records(limit=500)
            if record.status in _ACTIVE_ONLY
        ]
        if user_id is not None:
            records = [record for record in records if record.user_id == user_id]
        if memory_type is not None:
            records = [record for record in records if record.memory_type is memory_type]
        if not records:
            return [], 0.0
        ranked = rank_records(records, task, intent=intent)
        best = ranked[0][1] if ranked else 0.0
        candidates: list[_Candidate] = []
        for record, score, signals in ranked:
            if score < MIN_FINAL_SCORE:
                continue
            reasons = [f"{name} {value:.2f}" for name, value in signals.items() if value >= 0.25]
            candidates.append(
                _Candidate(
                    memory=RetrievedMemory(
                        kind="long_term",
                        id=record.id,
                        content=self._render_record(record),
                        score=score,
                        source=record.source,
                        trust_level=record.trust_level,
                        metadata={
                            "category": record.category,
                            "tags": record.tags,
                            "importance": record.importance,
                            "confidence": record.confidence.value,
                            "status": record.status.value,
                            "version": record.version,
                            "memory_type": record.memory_type.value,
                            "user_id": record.user_id,
                            "signals": signals,
                        },
                    ),
                    score=score,
                    reason_bits=reasons or ["weak match"],
                )
            )
        return candidates, best

    @staticmethod
    def _render_record(record: LongTermRecord) -> str:
        tags = f" [{', '.join(record.tags)}]" if record.tags else ""
        return f"{record.content} (category: {record.category}{tags})"

    def _episodic_candidates(self, task: str, session_id: str) -> tuple[list[_Candidate], float]:
        query = EpisodicQuery(session_id=session_id, limit=80)
        result = self.episodic.read(query)
        events = [view.episode for view in result.events]
        if not events:
            return [], 0.0
        now = datetime.now(UTC)
        task_tokens = _keywords(task)
        keyword = _normalise(
            [
                float(len(task_tokens & _keywords(f"{event.event_type} {event.summary}")))
                for event in events
            ]
        )
        recency = [_recency(event.timestamp, now, half_life_days=14.0) for event in events]
        candidates: list[_Candidate] = []
        best = 0.0
        for index, episode in enumerate(events):
            score = 0.65 * keyword[index] + 0.35 * recency[index]
            best = max(best, score)
            if score < MIN_EPISODIC_SCORE:
                continue
            reasons = []
            if keyword[index] >= 0.25:
                reasons.append(f"keyword {keyword[index]:.2f}")
            if recency[index] >= 0.5:
                reasons.append("recent event")
            candidates.append(
                _Candidate(
                    memory=RetrievedMemory(
                        kind="episodic",
                        id=episode.event_id,
                        content=f"{episode.event_type}: {episode.summary}",
                        score=score,
                        source=episode.source,
                        trust_level=TrustLevel.UNTRUSTED,
                        metadata={
                            "event_type": episode.event_type,
                            "timestamp": episode.timestamp,
                            "outcome": episode.outcome,
                            "signals": {
                                "keyword": round(keyword[index], 4),
                                "recency": round(recency[index], 4),
                            },
                        },
                    ),
                    score=score,
                    reason_bits=reasons,
                )
            )
        return candidates, best


__all__ = [
    "MIN_FINAL_SCORE",
    "MemoryRetriever",
    "QueryIntent",
    "detect_intent",
    "rank_records",
]
