"""Validation and sanitisation before anything is persisted (spec §5, §22.7, §22.11).

Every write path funnels through :class:`MemoryValidator`:

* schema and length limits (a model must not be able to write an unbounded payload),
* secret scanning with ``reject`` (default) or explicit ``redact`` policy,
* preservation of unfinished Working Memory tasks,
* provenance defaults that the model is not allowed to inflate (``confidence`` and
  ``importance`` are clamped by rules, not by self-assessment, spec §22.11).

Validation returns *new* objects; it never mutates the input document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import SecretDetectedError, ValidationError
from nanomemory.core.memory_models import (
    Confidence,
    Episode,
    LongTermRecord,
    MemoryUpdateProposal,
    SourceType,
    TrustLevel,
    WorkingMemoryDocument,
)
from nanomemory.core.secret_scanner import SecretScanner

MAX_GOAL_CHARS = 2000
MAX_ITEM_CHARS = 1000
MAX_LIST_ITEMS = 100


@dataclass(slots=True)
class ValidationOutcome:
    """Validated value plus scanner metadata for audit events (never raw secrets)."""

    value: Any
    secret_hits: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class MemoryValidator:
    """Stateless validator bound to configuration and a secret scanner."""

    def __init__(self, settings: Settings, scanner: SecretScanner | None = None) -> None:
        self.settings = settings
        self.scanner = scanner or SecretScanner(
            extra_allowlist_regex=settings.secret_allowlist_regex
        )
        self.max_chars = settings.working_memory_max_chars

    # ------------------------------------------------------------------ helpers
    def sanitise_text(self, text: str, *, location: str = "text") -> ValidationOutcome:
        """Apply the secret policy to a single string."""
        result = self.scanner.scan_text(text, location=location)
        if not result.found:
            return ValidationOutcome(value=text)
        hit_types = result.hit_types()
        if self.settings.secret_scan_mode == "redact":
            redacted, _ = self.scanner.redact_text(text)
            return ValidationOutcome(value=redacted, secret_hits=hit_types)
        raise SecretDetectedError(hit_types, result.locations())

    def sanitise_value(self, value: Any, *, location: str = "payload") -> ValidationOutcome:
        """Apply the secret policy to a JSON-compatible structure."""
        result = self.scanner.scan_value(value, location=location)
        if not result.found:
            return ValidationOutcome(value=value)
        hit_types = result.hit_types()
        if self.settings.secret_scan_mode == "redact":
            redacted, _ = self.scanner.redact_value(value)
            return ValidationOutcome(value=redacted, secret_hits=hit_types)
        raise SecretDetectedError(hit_types, result.locations())

    # ------------------------------------------------------------------ working
    def validate_working_update(
        self,
        proposal: MemoryUpdateProposal,
        current: WorkingMemoryDocument,
    ) -> ValidationOutcome:
        """Merge, sanitise and sanity-check a Working Memory proposal."""
        merged = proposal.apply_to(current)
        notes: list[str] = []

        # Length and shape limits (before storage, never after).
        merged.current_goal = merged.current_goal.strip() or "尚未設定"
        merged.next_action = merged.next_action.strip() or "等待使用者輸入"
        if len(merged.current_goal) > MAX_GOAL_CHARS:
            raise ValidationError("current_goal exceeds the maximum length")
        if len(merged.next_action) > MAX_GOAL_CHARS:
            raise ValidationError("next_action exceeds the maximum length")
        for field_name in ("relevant_context", "completed", "in_progress", "blockers"):
            items = getattr(merged, field_name)
            if len(items) > MAX_LIST_ITEMS:
                raise ValidationError(f"{field_name} exceeds {MAX_LIST_ITEMS} items")
            cleaned: list[str] = []
            for item in items:
                stripped = item.strip()
                if not stripped:
                    continue
                if len(stripped) > MAX_ITEM_CHARS:
                    raise ValidationError(f"{field_name} item exceeds {MAX_ITEM_CHARS} chars")
                if stripped not in cleaned:
                    cleaned.append(stripped)
            setattr(merged, field_name, cleaned)

        # Unfinished tasks may not silently disappear (spec §4.1).
        if not proposal.allow_task_removal:
            new_in_progress = set(merged.in_progress)
            retained = set(merged.completed) | set(merged.blockers)
            dropped = [
                item
                for item in current.in_progress
                if item not in new_in_progress and item not in retained
            ]
            if dropped:
                raise ValidationError(
                    "proposal would drop unfinished tasks without marking them completed "
                    "or blocked; set allow_task_removal=true to override",
                    details={"dropped_items": dropped[:10]},
                )
        elif any(item not in merged.in_progress for item in current.in_progress):
            notes.append("unfinished tasks removed with explicit allow_task_removal")

        # Secret policy over every free-text field.
        hits: list[str] = []
        for field_name in ("current_goal", "next_action"):
            outcome = self.sanitise_text(getattr(merged, field_name), location=field_name)
            setattr(merged, field_name, outcome.value)
            hits.extend(outcome.secret_hits)
        for field_name in ("relevant_context", "completed", "in_progress", "blockers"):
            cleaned_items: list[str] = []
            for index, item in enumerate(getattr(merged, field_name)):
                outcome = self.sanitise_text(item, location=f"{field_name}[{index}]")
                cleaned_items.append(outcome.value)
                hits.extend(outcome.secret_hits)
            setattr(merged, field_name, cleaned_items)

        total_chars = (
            len(merged.current_goal)
            + len(merged.next_action)
            + sum(
                len(item)
                for field_name in ("relevant_context", "completed", "in_progress", "blockers")
                for item in getattr(merged, field_name)
            )
        )
        if total_chars > self.max_chars:
            raise ValidationError(
                "working memory exceeds the configured character budget; compress before writing",
                details={"chars": total_chars, "limit": self.max_chars},
            )
        return ValidationOutcome(value=merged, secret_hits=sorted(set(hits)), notes=notes)

    # ------------------------------------------------------------------ long term
    def validate_long_term_record(self, payload: dict[str, Any]) -> ValidationOutcome:
        """Validate a new/updated long-term record payload."""
        source_type = payload.get("source_type", SourceType.UNKNOWN.value)
        trust_level = payload.get("trust_level", TrustLevel.UNKNOWN.value)

        outcome = self.sanitise_value(payload, location="long_term")
        cleaned = dict(outcome.value)
        cleaned["source_type"] = source_type
        cleaned["trust_level"] = self._clamp_trust(source_type, trust_level)
        cleaned["confidence"] = self._clamp_confidence(
            str(payload.get("confidence", Confidence.MEDIUM.value)), source_type
        )
        cleaned["importance"] = self._clamp_importance(
            int(payload.get("importance", 3)), source_type
        )
        try:
            record = LongTermRecord.model_validate(cleaned)
        except Exception as exc:  # pydantic ValidationError -> typed error
            raise ValidationError(
                "long-term memory payload is invalid",
                details={"reason": str(exc).splitlines()[0][:300]},
            ) from exc
        return ValidationOutcome(value=record, secret_hits=outcome.secret_hits)

    def _clamp_trust(self, source_type: str, requested: str) -> str:
        """The model may not label its own output as verified."""
        allowed = {
            SourceType.USER.value: {
                TrustLevel.USER_PROVIDED.value,
                TrustLevel.VERIFIED.value,
            },
            SourceType.SYSTEM.value: {
                TrustLevel.VERIFIED.value,
                TrustLevel.UNTRUSTED.value,
            },
            SourceType.TOOL.value: {
                TrustLevel.UNTRUSTED.value,
                TrustLevel.UNKNOWN.value,
            },
            SourceType.IMPORT.value: {TrustLevel.UNTRUSTED.value, TrustLevel.UNKNOWN.value},
            SourceType.MODEL.value: {TrustLevel.UNTRUSTED.value, TrustLevel.UNKNOWN.value},
        }.get(source_type, {TrustLevel.UNKNOWN.value, TrustLevel.UNTRUSTED.value})
        return requested if requested in allowed else TrustLevel.UNKNOWN.value

    def _clamp_confidence(self, requested: str, source_type: str) -> str:
        if requested not in {item.value for item in Confidence}:
            requested = Confidence.LOW.value
        if source_type in {SourceType.MODEL.value, SourceType.UNKNOWN.value}:
            return Confidence.LOW.value if requested == Confidence.HIGH.value else requested
        if source_type != SourceType.USER.value and requested == Confidence.HIGH.value:
            return Confidence.MEDIUM.value
        return requested

    def _clamp_importance(self, requested: int, source_type: str) -> int:
        value = min(5, max(1, int(requested)))
        if source_type in {SourceType.MODEL.value, SourceType.UNKNOWN.value}:
            return min(value, 3)
        return value

    # ------------------------------------------------------------------ episodic
    def validate_episode(self, episode: Episode) -> ValidationOutcome:
        """Scan summary/payload of an episode; ``source`` is also scanned (§22.7)."""
        hits: list[str] = []
        summary_outcome = self.sanitise_text(episode.summary, location="episode.summary")
        episode.summary = summary_outcome.value
        hits.extend(summary_outcome.secret_hits)

        source_outcome = self.sanitise_text(episode.source, location="episode.source")
        episode.source = source_outcome.value
        hits.extend(source_outcome.secret_hits)

        if episode.payload is not None:
            payload_outcome = self.sanitise_value(episode.payload, location="episode.payload")
            episode.payload = payload_outcome.value
            hits.extend(payload_outcome.secret_hits)
        return ValidationOutcome(value=episode, secret_hits=sorted(set(hits)))

    # ------------------------------------------------------------------ misc
    def validate_query(self, value: str, *, name: str, max_length: int = 200) -> str:
        """Validate a short user-supplied query string (no secrets, bounded)."""
        if len(value) > max_length:
            raise ValidationError(f"{name} exceeds {max_length} characters")
        outcome = self.sanitise_text(value, location=name)
        return str(outcome.value)


__all__ = ["MAX_ITEM_CHARS", "MAX_LIST_ITEMS", "MemoryValidator", "ValidationOutcome"]
