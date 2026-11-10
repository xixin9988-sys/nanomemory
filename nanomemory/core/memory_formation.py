"""Automatic memory formation (spec §3 of the 2.0 plan).

The pipeline is deliberately **deterministic first**: rules extract candidate memories,
the validator decides what may be written, and the store enforces the per-type write rules
(user-stated statements may become ``active``; model inferences become
``pending_confirmation``). An LLM can be plugged in later to *propose* candidates, but it never
gets to commit them directly — automatic extraction is not automatic belief.

Pipeline::

    text → extraction → candidates → validation → duplicate/conflict check
         → create | merge/supersede | pending → persistence → audit event
"""

from __future__ import annotations

import re
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_models import (
    Confidence,
    LongTermRecord,
    MemoryStatus,
    MemoryType,
    Provenance,
    SourceKind,
    utc_now_iso,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？.!?\n])\s*")

_PREFERENCE = re.compile(
    r"\b(i|we)\s+(really\s+)?(prefer|like|love|hate|dislike|favour|favor)\b"
    r"|我(比較|比较)?(喜歡|喜欢|偏好|不喜歡|不喜欢|討厭|讨厌)"
    r"|(偏好|习惯|習慣)",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"\b(i|we)\s+(decided|chose|picked|switched|migrated)\b|\bdecided to\b"
    r"|(決定|决定|改用|改成|選擇了|选择了|decided)",
    re.IGNORECASE,
)
_PROCEDURE = re.compile(
    r"\b(step\s*\d|first,|then,|how to|run\s+|command:)\b|(步驟|步骤|流程|先.*再)",
    re.IGNORECASE,
)
_FACT = re.compile(
    r"\b(is|are|uses|use|runs on|deployed|target|depends on)\b"
    r"|(是|使用|採用|采用|部署|目標|目标|依賴|依赖)",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"[?？]|^\s*(what|why|how|when|where|who|which|can|should|do|does)\b", re.I)
_NUMBERED = re.compile(r"^\s*(\d+[.)]|[-*])\s+")

MIN_CANDIDATE_LENGTH = 12
MAX_CANDIDATE_LENGTH = 400


def classify(text: str) -> MemoryType | None:
    """Return the most plausible memory type, or ``None`` for non-memory sentences."""
    if _QUESTION.search(text):
        return None
    if _PREFERENCE.search(text):
        return MemoryType.PREFERENCE
    if _DECISION.search(text):
        return MemoryType.EVENT
    if _PROCEDURE.search(text) or _NUMBERED.search(text):
        return MemoryType.PROCEDURE
    if _FACT.search(text):
        return MemoryType.FACT
    return None


def extract_candidates(text: str, *, source_kind: SourceKind) -> list[dict[str, Any]]:
    """Extract candidate memories from free text with explainable rules."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = " ".join(raw.split()).strip(" -•\t")
        if not (MIN_CANDIDATE_LENGTH <= len(sentence) <= MAX_CANDIDATE_LENGTH):
            continue
        if _QUESTION.search(sentence):
            continue
        memory_type = classify(sentence)
        if memory_type is None:
            continue
        normalised = sentence.lower()
        if normalised in seen:
            continue
        seen.add(normalised)
        importance = {
            MemoryType.PREFERENCE: 4,
            MemoryType.PROCEDURE: 4,
            MemoryType.EVENT: 4,
            MemoryType.FACT: 3,
        }[memory_type]
        confidence = Confidence.HIGH if source_kind is SourceKind.USER else Confidence.LOW
        candidates.append(
            {
                "content": sentence,
                "memory_type": memory_type,
                "importance": importance,
                "confidence": confidence,
                "tags": [memory_type.value],
                "category": "general",
            }
        )
    return candidates


class MemoryFormation:
    """Extract, validate and store memories from a block of text."""

    def __init__(self, settings: Settings, long_term: LongTermMemoryStore) -> None:
        self.settings = settings
        self.long_term = long_term

    def form(
        self,
        text: str,
        *,
        user_id: str = "local",
        source_kind: SourceKind = SourceKind.USER,
        source_ref: str | None = None,
        session_id: str = "system",
        confirm: bool | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        candidates = extract_candidates(text, source_kind=source_kind)
        report: dict[str, Any] = {
            "extracted": len(candidates),
            "created": 0,
            "pending": 0,
            "conflicts": 0,
            "superseded": 0,
            "rejected": 0,
            "items": [],
            "dry_run": dry_run,
            "user_id": user_id,
        }
        for candidate in candidates:
            payload = {
                "content": candidate["content"],
                "category": candidate["category"],
                "memory_type": candidate["memory_type"].value,
                "importance": candidate["importance"],
                "confidence": candidate["confidence"].value,
                "tags": candidate["tags"],
                "source_type": "user" if source_kind is SourceKind.USER else source_kind.value,
                "source": source_ref or "conversation",
                "user_id": user_id,
                "valid_from": utc_now_iso(),
                "provenance": Provenance(
                    source_kind=source_kind,
                    source_ref=source_ref,
                    created_by="formation",
                    note="extracted by deterministic formation rules",
                ).model_dump(mode="json"),
            }
            if dry_run:
                report["items"].append(
                    {
                        "content": payload["content"],
                        "memory_type": payload["memory_type"],
                        "status": "dry_run",
                    }
                )
                continue
            try:
                record: LongTermRecord = self.long_term.add(
                    payload, session_id=session_id, confirm=confirm
                )
            except Exception as error:  # secret rejection / capacity / validation
                report["rejected"] += 1
                report["items"].append(
                    {
                        "content": payload["content"],
                        "memory_type": payload["memory_type"],
                        "status": "rejected",
                        "reason": str(error)[:200],
                    }
                )
                continue
            if record.status is MemoryStatus.PENDING_CONFIRMATION:
                report["pending"] += 1
            elif record.status is MemoryStatus.CONFLICT:
                report["conflicts"] += 1
            else:
                report["created"] += 1
            if record.supersedes:
                report["superseded"] += len(record.supersedes)
            report["items"].append(
                {
                    "id": record.id,
                    "content": record.content,
                    "memory_type": record.memory_type.value,
                    "status": record.status.value,
                    "confidence": record.confidence.value,
                    "supersedes": record.supersedes,
                }
            )
        return report


__all__ = ["MemoryFormation", "classify", "extract_candidates"]
