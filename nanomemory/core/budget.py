"""Agent run budgets (spec §22.10, §22.17).

A budget is checked **before** every provider and tool call.  Once exhausted, the loop stops
calling anything and returns ``budget_exceeded`` with the consumed amounts.  Cost budgets are
only enforceable when a price table is configured — otherwise the cost component is reported
as unenforceable instead of pretending to enforce it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError


@dataclass(slots=True)
class BudgetLimits:
    """Effective limits for one agent run."""

    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    max_wall_time_seconds: float | None = None
    max_cost: float | None = None
    cost_currency: str = "USD"
    price_table_configured: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetLimits:
        return cls(
            max_total_tokens=settings.agent_max_total_tokens or None,
            max_tool_calls=settings.agent_max_tool_calls or None,
            max_wall_time_seconds=settings.agent_max_wall_time_seconds or None,
            max_cost=settings.agent_max_cost or None,
            cost_currency=settings.cost_currency,
            price_table_configured=bool(settings.price_table_json.strip()),
        )


@dataclass(slots=True)
class BudgetTracker:
    """Mutable consumption counter with an explicit ``check`` gate."""

    limits: BudgetLimits
    tokens_used: int = 0
    tool_calls_used: int = 0
    cost_used: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------------ consumption
    def record_llm(self, *, tokens: int, cost: float | None = None) -> None:
        self.tokens_used += max(0, tokens)
        if cost is not None:
            self.cost_used += max(0.0, cost)

    def record_tool(self) -> None:
        self.tool_calls_used += 1

    @property
    def wall_time_seconds(self) -> float:
        return time.monotonic() - self.started_at

    # ------------------------------------------------------------------ checks
    def check(self, *, stage: str) -> None:
        """Raise ``budget_exceeded`` when any enforceable limit is reached."""
        if (
            self.limits.max_total_tokens is not None
            and self.tokens_used >= self.limits.max_total_tokens
        ):
            self._raise("max_total_tokens", self.tokens_used, self.limits.max_total_tokens, stage)
        if (
            self.limits.max_tool_calls is not None
            and self.tool_calls_used >= self.limits.max_tool_calls
        ):
            self._raise("max_tool_calls", self.tool_calls_used, self.limits.max_tool_calls, stage)
        if (
            self.limits.max_wall_time_seconds is not None
            and self.wall_time_seconds >= self.limits.max_wall_time_seconds
        ):
            self._raise(
                "max_wall_time",
                round(self.wall_time_seconds, 3),
                self.limits.max_wall_time_seconds,
                stage,
            )
        if (
            self.limits.max_cost is not None
            and self.limits.price_table_configured
            and self.cost_used >= self.limits.max_cost
        ):
            self._raise("max_cost", round(self.cost_used, 8), self.limits.max_cost, stage)

    def _raise(self, kind: str, consumed: float | int, limit: float | int, stage: str) -> None:
        raise NanoMemoryError(
            code=ErrorCode.BUDGET_EXCEEDED,
            message=f"Agent budget exhausted ({kind}) before {stage}.",
            details={
                "budget": kind,
                "consumed": consumed,
                "limit": limit,
                "stage": stage,
                "tokens_used": self.tokens_used,
                "tool_calls_used": self.tool_calls_used,
                "cost_used": round(self.cost_used, 8),
                "wall_time_seconds": round(self.wall_time_seconds, 3),
                "cost_enforceable": bool(
                    self.limits.max_cost is not None and self.limits.price_table_configured
                ),
            },
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens_used": self.tokens_used,
            "tool_calls_used": self.tool_calls_used,
            "cost_used": round(self.cost_used, 8),
            "cost_currency": self.limits.cost_currency,
            "wall_time_seconds": round(self.wall_time_seconds, 3),
            "limits": {
                "max_total_tokens": self.limits.max_total_tokens,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_wall_time_seconds": self.limits.max_wall_time_seconds,
                "max_cost": self.limits.max_cost,
                "price_table_configured": self.limits.price_table_configured,
            },
        }


__all__ = ["BudgetLimits", "BudgetTracker"]
