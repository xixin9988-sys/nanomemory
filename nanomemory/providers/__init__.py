"""Provider factory: mock by default, explicit real provider otherwise (spec §22.20)."""

from __future__ import annotations

from nanomemory.core.config import Settings
from nanomemory.providers.base import (
    BaseProvider,
    LLMResponse,
    ProviderError,
    ProviderErrorCategory,
)
from nanomemory.providers.mock_provider import MockProvider
from nanomemory.providers.openai_compatible import OpenAICompatibleProvider


def build_provider(settings: Settings) -> BaseProvider:
    """Return the configured provider.

    ``mock`` is the default so a clean install runs without any API key.  Selecting
    ``openai_compatible`` without a key raises a configuration error instead of silently
    falling back to mock (which would misrepresent real model execution).
    """
    if settings.provider == "mock":
        return MockProvider(model=settings.model if settings.model != "mock-echo" else "mock-echo")
    return OpenAICompatibleProvider(settings)


__all__ = [
    "BaseProvider",
    "LLMResponse",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderErrorCategory",
    "build_provider",
]
