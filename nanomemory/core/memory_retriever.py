"""Memory retrieval with an explainable score and a hard token budget.

Scoring is deliberately simple, local and reproducible (no embeddings, no vector database
dependency):

* keyword overlap between the task and the memory content,
* tag match,
* category match,
* ``importance`` (1-5) and ``confidence``,
* recency for episodic items.

Every selected item carries a human readable ``reason``, and items dropped for budget or
relevance are recorded in ``omitted`` so the dashboard can explain the context.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.episodic_memory import EpisodicMemoryStore, EpisodicQuery
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import (
    Episode,
    LongTermRecord,
    MemoryStatus,
    RetrievalResult,
    RetrievedMemory,
    TrustLevel,
)
from nanomemory.core.token_counter import TokenCounter
from nanomemory.core.working_memory import WorkingMemoryStore

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
MIN_RELEVANCE_SCORE = 1.5
MAX_LONG_TERM_ITEMS = 8
MAX_EPISODIC_ITEMS = 5

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


def _keywords(text: str) -> set[str]:
    """Lower-cased keyword set used for overlap scoring (latin words + CJK bigrams)."""
    lowered = text.lower()
    tokens = {token for token in _WORD.findall(lowered) if token not in _STOPWORDS}
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    for phrase in cjk:
        for index in range(len(phrase) - 1):
            tokens.add(phrase[index : index + 2])
    return tokens


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
    ) -> RetrievalResult:
        budget = token_budget if token_budget is not None else self.settings.memory_token_limit
        budget = max(1, int(budget))
        task_keywords = _keywords(task)
        selected: list[RetrievedMemory] = []
        omitted: list[dict[str, Any]] = []
        used = 0
        result = RetrievalResult(
            current_task=task,
            session_id=session_id,
            token_budget=budget,
            retrieval_reason="",
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
        if include_long_term_memory:
            candidates.extend(self._long_term_candidates(task_keywords))
        if include_recent_episodes:
            candidates.extend(self._episodic_candidates(task_keywords, session_id))
        candidates.sort(key=lambda item: item.score, reverse=True)
        # Only a bounded number of items per layer: a memory that merely shares one word
        # must not consume the budget (spec §6: irrelevant memories are not auto-loaded).
        long_term_candidates = [item for item in candidates if item.memory.kind == "long_term"][
            :MAX_LONG_TERM_ITEMS
        ]
        episodic_candidates = [item for item in candidates if item.memory.kind == "episodic"][
            :MAX_EPISODIC_ITEMS
        ]
        candidates = sorted(
            long_term_candidates + episodic_candidates,
            key=lambda item: item.score,
            reverse=True,
        )

        for candidate in candidates:
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

        result.selected = selected
        result.omitted = omitted
        result.estimated_tokens = used
        result.retrieval_reason = (
            f"{len(selected)} memories selected for {len(task_keywords)} task keywords; "
            f"{len(omitted)} omitted; budget {budget} tokens"
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

    def _long_term_candidates(self, task_keywords: set[str]) -> list[_Candidate]:
        records = self.long_term.list_records(limit=200)
        candidates: list[_Candidate] = []
        for record in records:
            if record.status is MemoryStatus.ARCHIVED:
                continue
            score, reasons = self._score_record(record, task_keywords)
            if score <= 0:
                continue
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
                        },
                    ),
                    score=score,
                    reason_bits=reasons,
                )
            )
        return candidates

    @staticmethod
    def _render_record(record: LongTermRecord) -> str:
        tags = f" [{', '.join(record.tags)}]" if record.tags else ""
        return f"{record.content} (category: {record.category}{tags})"

    def _score_record(
        self, record: LongTermRecord, task_keywords: set[str]
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        record_keywords = _keywords(record.content) | set(record.tags) | _keywords(record.category)
        overlap = task_keywords & record_keywords
        score = 0.0
        if overlap:
            score += 3.0 * min(len(overlap), 5) / 5
            reasons.append(f"keyword overlap: {', '.join(sorted(overlap)[:5])}")
        tag_overlap = task_keywords & set(record.tags)
        if tag_overlap:
            score += 5.0
            reasons.append(f"tag match: {', '.join(sorted(tag_overlap))}")
        if record.category.lower() in {keyword for keyword in task_keywords}:
            score += 2.0
            reasons.append("category match")
        if record.importance >= 4:
            score += (record.importance - 3) * 0.5
            reasons.append(f"importance {record.importance}")
        if record.confidence.value == "high":
            score += 0.5
        # requires_review/conflict memories are still retrievable but ranked lower
        if record.status in {MemoryStatus.NEEDS_REVIEW, MemoryStatus.CONFLICT}:
            score -= 1.0
            reasons.append(f"status {record.status.value} (needs attention)")
        return score, reasons

    def _episodic_candidates(self, task_keywords: set[str], session_id: str) -> list[_Candidate]:
        query = EpisodicQuery(session_id=session_id, limit=50)
        result = self.episodic.read(query)
        candidates: list[_Candidate] = []
        now = datetime.now(UTC)
        for view in result.events:
            episode = view.episode
            score, reasons = self._score_episode(episode, task_keywords, now)
            if score <= 0:
                continue
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
                        },
                    ),
                    score=score,
                    reason_bits=reasons,
                )
            )
        return candidates

    @staticmethod
    def _score_episode(
        episode: Episode, task_keywords: set[str], now: datetime
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        overlap = task_keywords & _keywords(f"{episode.event_type} {episode.summary}")
        score = 0.0
        if overlap:
            score += 2.0 * min(len(overlap), 4) / 4
            reasons.append(f"keyword overlap: {', '.join(sorted(overlap)[:4])}")
        try:
            stamp = datetime.fromisoformat(episode.timestamp)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age_days = max(0.0, (now - stamp).total_seconds() / 86400)
        except ValueError:
            age_days = 365.0
        recency = math.exp(-age_days / 14)
        score += 2.0 * recency
        if recency > 0.5:
            reasons.append("recent event")
        if score < 0.6:
            return 0.0, []
        return score, reasons


__all__ = ["MemoryRetriever"]
