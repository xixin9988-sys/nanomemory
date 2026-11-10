"""Working Memory compression with a structural fidelity check (spec §7, §22.15).

Two modes:

``deterministic`` (default)
    Rule-based clean-up: deduplicate list items, normalise whitespace, cap list lengths,
    mark over-long items as truncated.  No fact is invented and no fact is deleted: items
    that would be removed are recorded in the diff, and if the result still exceeds the
    character budget the compression is **abandoned** rather than forced.

``llm_assisted``
    Disabled by default (``NANOMEMORY_COMPRESSOR_LLM_ASSISTED``).  The model proposes a
    compressed document; the program then

    * checks that goal, blockers, next action and every unfinished item survived,
    * rejects any new "fact" that has no declared source,
    * checks the result still fits the budget,
    * and, when anything is uncertain, keeps the original document and marks it
      ``needs_review`` instead of silently applying the model's output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.memory_models import GoalStatus, WorkingMemoryDocument
from nanomemory.core.token_counter import TokenCounter

MAX_LIST_LENGTHS: dict[str, int] = {
    "relevant_context": 15,
    "completed": 20,
    "in_progress": 30,
    "blockers": 10,
}
MAX_ITEM_CHARS = 400
CRITICAL_FIELDS = ("current_goal", "next_action", "blockers", "in_progress")


@dataclass(slots=True)
class FieldDiff:
    """Per-field structured diff between the original and compressed document."""

    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    changed_scalar: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.added or self.truncated or self.changed_scalar)

    def as_dict(self) -> dict[str, Any]:
        return {
            "removed": self.removed,
            "added": self.added,
            "truncated": self.truncated,
            "changed_scalar": self.changed_scalar,
        }


@dataclass(slots=True)
class CompressionResult:
    """Outcome of a compression attempt."""

    applied: bool
    mode: str
    document: WorkingMemoryDocument
    original: WorkingMemoryDocument
    diff: dict[str, FieldDiff] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    chars_before: int = 0
    chars_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    needs_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "mode": self.mode,
            "needs_review": self.needs_review,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "warnings": self.warnings,
            "diff": {name: diff.as_dict() for name, diff in self.diff.items()},
        }


class MemoryCompressor:
    """Deterministic and (opt-in) LLM-assisted Working Memory compression."""

    def __init__(
        self,
        settings: Settings,
        counter: TokenCounter | None = None,
        provider: Any | None = None,
    ) -> None:
        self.settings = settings
        self.counter = counter or TokenCounter(settings)
        self.provider = provider

    # ------------------------------------------------------------------ policy
    def should_compress(self, document: WorkingMemoryDocument) -> bool:
        """Compress when the document approaches the configured character budget."""
        text = self._render(document)
        return len(text) >= int(
            self.settings.working_memory_max_chars * self.settings.compressor_trigger_ratio
        )

    # ------------------------------------------------------------------ entry point
    def compress(
        self,
        document: WorkingMemoryDocument,
        *,
        mode: str = "deterministic",
        source: str = "compressor",
    ) -> CompressionResult:
        before_text = self._render(document)
        if mode == "deterministic":
            return self._compress_deterministic(document, before_text)
        if mode == "llm_assisted":
            return self._compress_llm_assisted(document, before_text, source=source)
        raise NanoMemoryError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"Unknown compression mode: {mode}",
        )

    # ------------------------------------------------------------------ deterministic
    def _compress_deterministic(
        self, document: WorkingMemoryDocument, before_text: str
    ) -> CompressionResult:
        candidate = document.model_copy(deep=True)
        diff: dict[str, FieldDiff] = {}
        warnings: list[str] = []

        for field_name, limit in MAX_LIST_LENGTHS.items():
            original_items = list(getattr(document, field_name))
            cleaned: list[str] = []
            field_diff = FieldDiff()
            for item in original_items:
                normalised = " ".join(item.split())
                if not normalised:
                    field_diff.removed.append(item)
                    continue
                if normalised not in cleaned:
                    cleaned.append(normalised)
                else:
                    field_diff.removed.append(item)
                if len(normalised) > MAX_ITEM_CHARS:
                    field_diff.truncated.append(normalised)
            if len(cleaned) > limit:
                # Keep the newest items for `completed`, the first ones elsewhere.
                keep = cleaned[-limit:] if field_name == "completed" else cleaned[:limit]
                field_diff.removed.extend(item for item in cleaned if item not in keep)
                cleaned = keep
            setattr(candidate, field_name, cleaned)
            diff[field_name] = field_diff

        goal = " ".join(document.current_goal.split())
        candidate.current_goal = goal or "尚未設定"
        next_action = " ".join(document.next_action.split())
        candidate.next_action = next_action or "等待使用者輸入"
        diff["current_goal"] = FieldDiff(changed_scalar=goal != document.current_goal)
        diff["next_action"] = FieldDiff(changed_scalar=next_action != document.next_action)
        if candidate.status is GoalStatus.BLOCKED and not candidate.blockers:
            candidate.blockers = document.blockers

        after_text = self._render(candidate)
        if len(after_text) > self.settings.working_memory_max_chars:
            return CompressionResult(
                applied=False,
                mode="deterministic",
                document=document,
                original=document,
                diff=diff,
                warnings=[
                    "compression could not reduce the document below the character budget; "
                    "original kept"
                ],
                chars_before=len(before_text),
                chars_after=len(after_text),
                tokens_before=self.counter.estimate(before_text),
                tokens_after=self.counter.estimate(after_text),
                needs_review=True,
            )

        fidelity_warnings = self._check_fidelity(document, candidate)
        if fidelity_warnings:
            return CompressionResult(
                applied=False,
                mode="deterministic",
                document=document,
                original=document,
                diff=diff,
                warnings=fidelity_warnings,
                chars_before=len(before_text),
                chars_after=len(after_text),
                tokens_before=self.counter.estimate(before_text),
                tokens_after=self.counter.estimate(after_text),
                needs_review=True,
            )
        if not any(item.changed for item in diff.values()):
            warnings.append("nothing to compress")
        return CompressionResult(
            applied=True,
            mode="deterministic",
            document=candidate,
            original=document,
            diff=diff,
            warnings=warnings,
            chars_before=len(before_text),
            chars_after=len(after_text),
            tokens_before=self.counter.estimate(before_text),
            tokens_after=self.counter.estimate(after_text),
        )

    # ------------------------------------------------------------------ llm assisted
    def _compress_llm_assisted(
        self,
        document: WorkingMemoryDocument,
        before_text: str,
        *,
        source: str,
    ) -> CompressionResult:
        if not self.settings.compressor_llm_assisted:
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=["llm_assisted compression is disabled by configuration"],
                chars_before=len(before_text),
                chars_after=len(before_text),
                tokens_before=self.counter.estimate(before_text),
                tokens_after=self.counter.estimate(before_text),
            )
        if self.provider is None:
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=["no provider configured for llm_assisted compression"],
                needs_review=True,
            )
        prompt = (
            "Compress the following working memory document without inventing or deleting "
            "facts. Return JSON with the same keys plus a 'sources' object mapping every new "
            "or changed list item to the identifier of the memory it came from.\n\n" + before_text
        )
        try:
            raw = self.provider.generate(prompt=prompt)  # type: ignore[attr-defined]
            payload = json.loads(raw if isinstance(raw, str) else raw.content)
            if not isinstance(payload, dict):
                raise ValueError("compression output must be a JSON object")
            sources = payload.pop("sources", None)
            candidate = WorkingMemoryDocument.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - model output is untrusted
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=[f"model output could not be validated: {type(exc).__name__}"],
                chars_before=len(before_text),
                chars_after=len(before_text),
                tokens_before=self.counter.estimate(before_text),
                tokens_after=self.counter.estimate(before_text),
                needs_review=True,
            )

        new_items = [
            item
            for field_name in ("completed", "in_progress", "blockers", "relevant_context")
            for item in getattr(candidate, field_name)
            if item not in getattr(document, field_name)
        ]
        unsourced = [
            item
            for item in new_items
            if not isinstance(sources, dict) or item not in sources or not sources[item]
        ]
        if unsourced:
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=[
                    "model introduced facts without a source; original kept",
                    f"unsourced_count={len(unsourced)}",
                ],
                needs_review=True,
            )
        fidelity_warnings = self._check_fidelity(document, candidate)
        if fidelity_warnings:
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=fidelity_warnings,
                needs_review=True,
            )
        after_text = self._render(candidate)
        if len(after_text) > self.settings.working_memory_max_chars:
            return CompressionResult(
                applied=False,
                mode="llm_assisted",
                document=document,
                original=document,
                warnings=["compressed document exceeds the character budget; original kept"],
                needs_review=True,
            )
        diff = self._build_diff(document, candidate)
        return CompressionResult(
            applied=True,
            mode="llm_assisted",
            document=candidate,
            original=document,
            diff=diff,
            chars_before=len(before_text),
            chars_after=len(after_text),
            tokens_before=self.counter.estimate(before_text),
            tokens_after=self.counter.estimate(after_text),
        )

    # ------------------------------------------------------------------ fidelity
    @staticmethod
    def _check_fidelity(
        original: WorkingMemoryDocument, candidate: WorkingMemoryDocument
    ) -> list[str]:
        """Return human readable violations; empty means faithful."""
        warnings: list[str] = []
        if not candidate.current_goal.strip():
            warnings.append("current_goal would be lost")
        if not candidate.next_action.strip():
            warnings.append("next_action would be lost")
        if original.blockers and not candidate.blockers:
            warnings.append("blockers would be lost while the original had blockers")
        retained = set(candidate.in_progress) | set(candidate.completed) | set(candidate.blockers)
        lost = [item for item in original.in_progress if item not in retained]
        if lost:
            warnings.append(f"unfinished items would be lost: {len(lost)}")
        return warnings

    def _build_diff(
        self, original: WorkingMemoryDocument, candidate: WorkingMemoryDocument
    ) -> dict[str, FieldDiff]:
        diff: dict[str, FieldDiff] = {}
        for field_name in (
            "relevant_context",
            "completed",
            "in_progress",
            "blockers",
        ):
            before = getattr(original, field_name)
            after = getattr(candidate, field_name)
            diff[field_name] = FieldDiff(
                removed=[item for item in before if item not in after],
                added=[item for item in after if item not in before],
            )
        for field_name in ("current_goal", "next_action", "status"):
            diff[field_name] = FieldDiff(
                changed_scalar=getattr(original, field_name) != getattr(candidate, field_name)
            )
        return diff

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def _render(document: WorkingMemoryDocument) -> str:
        from nanomemory.core.memory_models import render_working_markdown

        return render_working_markdown(document)


__all__ = [
    "CRITICAL_FIELDS",
    "CompressionResult",
    "FieldDiff",
    "MAX_ITEM_CHARS",
    "MAX_LIST_LENGTHS",
    "MemoryCompressor",
]
