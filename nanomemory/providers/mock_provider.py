"""Deterministic mock provider for offline demos and tests (spec §22.20, §25.4).

Behaviour is fully deterministic:

* ``echo`` (default) - returns a fixed JSON object summarising the task, ``tool_calls=[]``.
* ``scripted`` - returns the queued response fixtures in order; once exhausted it raises
  ``mock_script_exhausted`` instead of inventing content.
* ``fail_json`` - returns deliberately invalid JSON (parser/repair path tests).
* ``timeout`` - raises a timeout error immediately (no real waiting).
* ``rate_limit`` - raises a rate-limit error.

Mock usage is always marked as mock; it is never reported as provider-exact usage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.providers.base import (
    BaseProvider,
    LLMResponse,
    ProviderError,
    ProviderErrorCategory,
)

MOCK_TOOL_FREE_RESPONSE = {
    "response": "Mock provider completed the request without calling any tool.",
    "task_status": "in_progress",
    "next_action": "Review the mock response and continue the task.",
    "tool_calls": [],
    "memory_update": None,
}


@dataclass(slots=True)
class MockProvider(BaseProvider):
    """Deterministic provider with scripted responses and failure modes."""

    mode: str = "echo"
    model: str = "mock-echo"
    scripted_responses: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    memory_update: dict[str, Any] | None = None
    fail_after: int | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    provider_name: str = "mock"
    is_mock: bool = True
    supports_native_json_schema: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"echo", "scripted", "fail_json", "timeout", "rate_limit"}:
            raise ValueError(f"unknown mock provider mode: {self.mode}")

    @property
    def model_name(self) -> str:  # type: ignore[override]
        return self.model

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "mode": self.mode,
            }
        )
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise ProviderError(
                ProviderErrorCategory.UNKNOWN,
                "Mock provider configured to fail after a number of calls.",
                retryable=False,
            )
        if self.mode == "timeout":
            raise ProviderError(
                ProviderErrorCategory.TIMEOUT,
                "Mock provider timeout (simulated).",
                retryable=True,
            )
        if self.mode == "rate_limit":
            raise ProviderError(
                ProviderErrorCategory.RATE_LIMIT,
                "Mock provider rate limit (simulated).",
                retryable=True,
            )
        if self.mode == "fail_json":
            return LLMResponse(text="{ this is not valid json ", provider="mock", model=self.model)
        if self.mode == "scripted":
            if not self.scripted_responses:
                raise NanoMemoryError(
                    code=ErrorCode.PROVIDER_INVALID_RESPONSE,
                    message="mock_script_exhausted",
                    details={"reason": "mock_script_exhausted"},
                )
            text = self.scripted_responses.pop(0)
            return LLMResponse(text=text, provider="mock", model=self.model)
        return LLMResponse(text=self._echo_payload(prompt), provider="mock", model=self.model)

    # ------------------------------------------------------------------ helpers
    def _echo_payload(self, prompt: str) -> str:
        task = _extract_task(prompt)
        payload = dict(MOCK_TOOL_FREE_RESPONSE)
        payload["response"] = (
            "Mock provider received the task "
            f"({len(task)} chars) and produced a deterministic reply."
        )
        payload["next_action"] = f"Continue with: {task[:120]}" if task else payload["next_action"]
        payload["tool_calls"] = list(self.tool_calls)
        payload["memory_update"] = self.memory_update
        return json.dumps(payload, ensure_ascii=False)


_TASK_PATTERN = re.compile(r"<current_task>(.*?)</current_task>", re.DOTALL)


def _extract_task(prompt: str) -> str:
    match = _TASK_PATTERN.search(prompt)
    if match:
        return match.group(1).strip()
    return prompt.strip()[:200]


__all__ = ["MOCK_TOOL_FREE_RESPONSE", "MockProvider"]
