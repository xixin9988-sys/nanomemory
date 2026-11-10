"""OpenAI-compatible provider (OpenAI, DeepSeek and other compatible endpoints).

Credentials come only from settings (``NANOMEMORY_API_KEY``) and are never logged.  Provider
exceptions are classified into :class:`ProviderError` categories so callers can react (retry,
surface a 502, stop the loop) without seeing raw provider payloads.
"""

from __future__ import annotations

from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.logging_config import get_logger
from nanomemory.core.token_counter import ProviderUsage
from nanomemory.providers.base import (
    BaseProvider,
    LLMResponse,
    ProviderError,
    ProviderErrorCategory,
)


class OpenAICompatibleProvider(BaseProvider):
    """Thin async adapter over the OpenAI SDK chat completions API."""

    provider_name = "openai_compatible"
    is_mock = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = get_logger("provider.openai_compatible")
        if settings.api_key is None or not settings.api_key.get_secret_value().strip():
            raise NanoMemoryError(
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                message=(
                    "Provider 'openai_compatible' was selected but NANOMEMORY_API_KEY is not "
                    "set. Set the key, or select NANOMEMORY_PROVIDER=mock explicitly."
                ),
                details={"provider": settings.provider},
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise NanoMemoryError(
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                message="The openai package is not installed.",
            ) from exc
        self.model_name = settings.model
        self._client = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url or None,
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {"model": self.settings.model, "messages": messages}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - classified below, never re-raised raw
            raise self._classify(exc) from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                "Provider returned no choices.",
                retryable=True,
            )
        message = choices[0].message
        text = message.content or ""
        usage_block = getattr(response, "usage", None)
        usage = None
        if usage_block is not None:
            usage = ProviderUsage.from_mapping(
                {
                    "prompt_tokens": getattr(usage_block, "prompt_tokens", None),
                    "completion_tokens": getattr(usage_block, "completion_tokens", None),
                    "total_tokens": getattr(usage_block, "total_tokens", None),
                }
            )
        return LLMResponse(
            text=text,
            provider=self.provider_name,
            model=getattr(response, "model", self.settings.model),
            usage=usage,
            finish_reason=getattr(choices[0], "finish_reason", None),
        )

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 - closing must never fail a request
            return

    # ------------------------------------------------------------------ classification
    def _classify(self, exc: BaseException) -> ProviderError:
        import openai

        name = type(exc).__name__
        status_code = getattr(exc, "status_code", None)
        category: ProviderErrorCategory
        retryable: bool
        if isinstance(exc, openai.AuthenticationError) or status_code in {401, 403}:
            category, retryable = ProviderErrorCategory.AUTHENTICATION, False
        elif isinstance(exc, openai.RateLimitError) or status_code == 429:
            category, retryable = ProviderErrorCategory.RATE_LIMIT, True
        elif isinstance(exc, openai.APITimeoutError):
            category, retryable = ProviderErrorCategory.TIMEOUT, True
        elif isinstance(exc, openai.APIConnectionError):
            category, retryable = ProviderErrorCategory.CONNECTION, True
        elif isinstance(exc, openai.BadRequestError) or status_code == 400:
            category, retryable = ProviderErrorCategory.INVALID_RESPONSE, True
        elif isinstance(exc, openai.APIStatusError):
            category = ProviderErrorCategory.UNKNOWN
            retryable = bool(status_code and 500 <= status_code < 600)
        else:
            category = ProviderErrorCategory.UNKNOWN
            retryable = False
        self.log.warning(
            "provider_error",
            extra={
                "event": "provider_error",
                "category": category.value,
                "exception_type": name,
                "status_code": status_code,
            },
        )
        return ProviderError(
            category,
            f"Provider call failed ({category.value}).",
            retryable=retryable,
            details={"status_code": status_code, "exception_type": name},
        )


__all__ = ["OpenAICompatibleProvider"]
