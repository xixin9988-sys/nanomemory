"""LLM provider interface and error taxonomy (spec §9, §22.20).

Providers never receive secrets from callers: credentials live in settings and are only read
inside the concrete adapter.  Errors are classified so the API can map them to stable codes
without leaking provider payloads, stack traces or headers.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.token_counter import ProviderUsage


class ProviderErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


_CATEGORY_TO_CODE: dict[ProviderErrorCategory, ErrorCode] = {
    ProviderErrorCategory.AUTHENTICATION: ErrorCode.PROVIDER_AUTH,
    ProviderErrorCategory.RATE_LIMIT: ErrorCode.PROVIDER_RATE_LIMIT,
    ProviderErrorCategory.TIMEOUT: ErrorCode.PROVIDER_TIMEOUT,
    ProviderErrorCategory.CONNECTION: ErrorCode.PROVIDER_CONNECTION,
    ProviderErrorCategory.INVALID_RESPONSE: ErrorCode.PROVIDER_INVALID_RESPONSE,
    ProviderErrorCategory.UNKNOWN: ErrorCode.PROVIDER_UNKNOWN,
}


class ProviderError(NanoMemoryError):
    """Classified provider failure (message never contains credentials or raw headers)."""

    def __init__(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"category": category.value}
        if retryable is not None:
            merged["retryable"] = retryable
        if details:
            merged.update(details)
        super().__init__(
            code=_CATEGORY_TO_CODE[category],
            message=message,
            details=merged,
        )
        self.category = category
        self.retryable = retryable


@dataclass(slots=True)
class LLMResponse:
    """One model completion plus integrity metadata."""

    text: str
    provider: str
    model: str
    usage: ProviderUsage | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class BaseProvider(ABC):
    """Provider interface: plain text and schema-validated structured output."""

    provider_name: str = "base"
    model_name: str = "unknown"
    supports_native_json_schema: bool = False
    is_mock: bool = False

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Generate a completion for ``prompt``."""

    async def generate_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_attempts: int = 3,
    ) -> tuple[BaseModel, LLMResponse]:
        """Generate and validate JSON output against ``schema`` with bounded retries."""
        attempt = 0
        last_error: Exception | None = None
        repair_prompt = prompt
        while attempt < max(1, max_attempts):
            attempt += 1
            response = await self.generate(
                repair_prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            try:
                payload = json.loads(_extract_json(response.text))
                return schema.model_validate(payload), response
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                repair_prompt = (
                    f"{prompt}\n\nYour previous reply was not valid JSON for the required "
                    f"schema ({type(exc).__name__}). Reply with the JSON object only."
                )
        raise ProviderError(
            ProviderErrorCategory.INVALID_RESPONSE,
            "Provider did not return valid structured output.",
            retryable=True,
            details={
                "attempts": attempt,
                "reason": type(last_error).__name__ if last_error else None,
            },
        )

    async def aclose(self) -> None:
        """Release provider resources (no-op by default)."""
        return None


def _extract_json(text: str) -> str:
    """Extract the first JSON object from a possibly fenced model reply."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            body = (
                "\n".join(lines[1:-1])
                if lines[-1].strip().startswith("```")
                else "\n".join(lines[1:])
            )
            stripped = body.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in provider output")
    return stripped[start : end + 1]


__all__ = [
    "BaseProvider",
    "LLMResponse",
    "ProviderError",
    "ProviderErrorCategory",
    "_extract_json",
]
