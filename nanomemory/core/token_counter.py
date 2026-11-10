"""Token estimation and cost accounting (spec §10, §22.12, §24.5, §29.5).

Honesty rules implemented here:

* ``usage_source="exact"`` is used **only** when the provider response actually reported
  token usage.  Everything else is ``estimated`` (local heuristic) or ``unknown``.
* A missing price table means cost is ``None`` and is rendered as "N/A" — never guessed.
* The estimator is named and reported so estimates are attributable and reproducible.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import UsageRecord

#: CJK characters cost roughly one token each; latin text roughly four characters per token.
_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\uac00-\ud7af]"
)
ESTIMATOR_NAME = "heuristic-cjk1-latin4-v1"
MESSAGE_OVERHEAD_TOKENS = 4


class TokenEstimator:
    """Deterministic local token estimate (never presented as provider truth)."""

    name = ESTIMATOR_NAME

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK.findall(text))
        remaining = len(text) - cjk
        # ceil keeps the estimate conservative (over-estimating is safer for budgets).
        return cjk + math.ceil(remaining / 4)

    def estimate_messages(self, messages: list[tuple[str, str]]) -> int:
        total = 0
        for _role, content in messages:
            total += self.estimate(content) + MESSAGE_OVERHEAD_TOKENS
        return total


@dataclass(slots=True)
class ModelPrice:
    """Price per 1000 tokens for one model."""

    model: str
    input_per_1k: float
    output_per_1k: float
    currency: str = "USD"


class PriceTable:
    """Price configuration loaded from ``NANOMEMORY_PRICE_TABLE_JSON``.

    Prices are configuration, not hard-coded facts: an empty or malformed table simply means
    "cost unknown", and the API reports ``N/A`` (never an invented price).
    """

    def __init__(self, raw_json: str = "") -> None:
        self.log = get_logger("pricing")
        self._prices: dict[str, ModelPrice] = {}
        self.error: str | None = None
        if raw_json.strip():
            self._load(raw_json)

    def _load(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.error = f"invalid price table JSON: {exc.msg}"
            self.log.warning("price_table_invalid", extra={"event": "price_table_invalid"})
            return
        if not isinstance(payload, dict):
            self.error = "price table must be a JSON object"
            return
        for model, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            try:
                price = ModelPrice(
                    model=str(model),
                    input_per_1k=float(entry["input_per_1k"]),
                    output_per_1k=float(entry["output_per_1k"]),
                    currency=str(entry.get("currency", "USD")),
                )
            except (KeyError, TypeError, ValueError):
                self.error = f"invalid price entry for model {model!r}"
                continue
            self._prices[str(model)] = price

    def price_for(self, model: str) -> ModelPrice | None:
        return self._prices.get(model)

    def cost(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> tuple[float, str] | None:
        price = self.price_for(model)
        if price is None:
            return None
        total = (prompt_tokens / 1000) * price.input_per_1k + (
            completion_tokens / 1000
        ) * price.output_per_1k
        return round(total, 8), price.currency

    @property
    def configured(self) -> bool:
        return bool(self._prices)


@dataclass(slots=True)
class ProviderUsage:
    """Usage block as reported by a provider (may be absent)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> ProviderUsage | None:
        if not payload:
            return None
        prompt = payload.get("prompt_tokens")
        completion = payload.get("completion_tokens")
        total = payload.get("total_tokens")
        if prompt is None and completion is None and total is None:
            return None
        return cls(
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=int(completion) if completion is not None else None,
            total_tokens=int(total) if total is not None else None,
        )


class TokenCounter:
    """Builds :class:`UsageRecord` objects with correct ``usage_source`` semantics."""

    def __init__(
        self,
        settings: Settings,
        *,
        estimator: TokenEstimator | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        self.settings = settings
        self.estimator = estimator or TokenEstimator()
        self.prices = prices or PriceTable(settings.price_table_json)

    def estimate(self, text: str) -> int:
        return self.estimator.estimate(text)

    def estimate_messages(self, messages: list[tuple[str, str]]) -> int:
        return self.estimator.estimate_messages(messages)

    def build_usage(
        self,
        *,
        provider: str,
        model: str,
        prompt_text: str,
        completion_text: str,
        latency_ms: float,
        provider_usage: ProviderUsage | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        kind: str = "agent",
        benchmark: str | None = None,
        mock: bool = False,
    ) -> UsageRecord:
        """Return a usage record; exact only when the provider reported numbers."""
        request_id = request_id or f"req-{uuid.uuid4().hex[:16]}"
        if mock:
            estimated_prompt = self.estimate(prompt_text)
            estimated_completion = self.estimate(completion_text)
            return UsageRecord(
                request_id=request_id,
                provider=provider,
                model=model,
                usage_source="mock",
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                estimated_tokens=estimated_prompt + estimated_completion,
                latency_ms=latency_ms,
                session_id=session_id,
                kind=kind,
                benchmark=benchmark,
                estimator=self.estimator.name,
            )

        if provider_usage is not None and (
            provider_usage.prompt_tokens is not None or provider_usage.completion_tokens is not None
        ):
            prompt = provider_usage.prompt_tokens or 0
            completion = provider_usage.completion_tokens or 0
            total = provider_usage.total_tokens or (prompt + completion)
            cost = self.prices.cost(model, prompt, completion)
            return UsageRecord(
                request_id=request_id,
                provider=provider,
                model=model,
                usage_source="exact",
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                estimated_tokens=None,
                latency_ms=latency_ms,
                cost=cost[0] if cost else None,
                cost_currency=cost[1] if cost else None,
                session_id=session_id,
                kind=kind,
                benchmark=benchmark,
                estimator=None,
            )

        estimated_prompt = self.estimate(prompt_text)
        estimated_completion = self.estimate(completion_text)
        cost = self.prices.cost(model, estimated_prompt, estimated_completion)
        return UsageRecord(
            request_id=request_id,
            provider=provider,
            model=model,
            usage_source="estimated",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            estimated_tokens=estimated_prompt + estimated_completion,
            latency_ms=latency_ms,
            cost=cost[0] if cost else None,
            cost_currency=cost[1] if cost else None,
            session_id=session_id,
            kind=kind,
            benchmark=benchmark,
            estimator=self.estimator.name,
        )

    def render_cost(self, record: UsageRecord) -> str:
        """Human readable cost label used by the dashboard and reports."""
        if record.cost is None or record.cost_currency is None:
            return "N/A"
        return f"{record.cost:.6f} {record.cost_currency}"

    def budget_snapshot(self) -> dict[str, Any]:
        """Configuration values surfaced by ``/health`` and benchmark metadata (§24.5)."""
        return {
            "context_token_budget": self.settings.context_token_budget,
            "memory_token_limit": self.settings.memory_token_limit,
            "request_token_budget": self.settings.request_token_budget,
            "context_budget_margin": self.settings.context_budget_margin,
            "token_estimator": self.estimator.name,
            "prices_configured": self.prices.configured,
            "price_table_error": self.prices.error,
            "cost_currency": self.settings.cost_currency,
        }


__all__ = [
    "ESTIMATOR_NAME",
    "MESSAGE_OVERHEAD_TOKENS",
    "ModelPrice",
    "PriceTable",
    "ProviderUsage",
    "TokenCounter",
    "TokenEstimator",
]
