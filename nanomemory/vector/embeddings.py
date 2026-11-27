"""Embedding provider abstraction.

Three implementations ship with NanoMemory:

* HashingEmbeddingProvider -- deterministic, dependency-free feature hashing.  It is the
  offline default and the test oracle.  It captures *lexical* overlap only (no semantic
  generalisation), and the docs say so instead of pretending otherwise.
* OpenAICompatibleEmbeddingProvider -- the production path for any /v1/embeddings endpoint
  (OpenAI, Azure-compatible gateways, local vLLM/TEI servers).
* CachingEmbeddingProvider -- decorator that removes duplicate spend inside one process.

The interface is deliberately tiny: batch in, vectors plus accounting out.  Callers never see
HTTP details, and provider failures are classified with the shared ProviderError taxonomy so
the API can map them to stable error codes.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections import OrderedDict

import httpx

from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.providers.base import ProviderError, ProviderErrorCategory
from nanomemory.vector.schemas import EmbeddingResult

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")
_SPACE = re.compile(r"\s+")


class EmbeddingProvider(ABC):
    """Batch text -> dense vector interface."""

    provider_name: str = "base"
    model_name: str = "unknown"
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a non-empty batch; order of the output matches the input."""

    async def aclose(self) -> None:
        return None

    def describe(self) -> dict[str, object]:
        return {"provider": self.provider_name, "model": self.model_name, "dim": self.dim}


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        return vector
    return [value / norm for value in vector]


def _features(text: str) -> dict[str, float]:
    """Word unigrams, word bigrams and CJK bigrams with sublinear term frequency."""
    lowered = _SPACE.sub(" ", text.lower()).strip()
    words = [token for token in _WORD.findall(lowered)]
    counts: dict[str, float] = {}
    for token in words:
        counts[f"w:{token}"] = counts.get(f"w:{token}", 0.0) + 1.0
    for first, second in zip(words, words[1:], strict=False):
        key = f"b:{first}_{second}"
        counts[key] = counts.get(key, 0.0) + 0.5
    for phrase in _CJK.findall(lowered):
        for index in range(len(phrase) - 1):
            key = f"c:{phrase[index : index + 2]}"
            counts[key] = counts.get(key, 0.0) + 1.0
    # Character trigrams over the whole normalised string keep short queries stable.
    squeezed = lowered.replace(" ", "")
    for index in range(max(0, len(squeezed) - 2)):
        key = f"t:{squeezed[index : index + 3]}"
        counts[key] = counts.get(key, 0.0) + 0.25
    return {key: 1.0 + math.log(value) for key, value in counts.items() if value > 0}


class HashingEmbeddingProvider(EmbeddingProvider):
    """Signed feature hashing (a.k.a. the hashing trick) with L2 normalisation.

    Deterministic across processes and platforms: the same text always yields the same vector,
    which is what makes evaluation runs and regression tests comparable.
    """

    provider_name = "hashing"

    def __init__(self, *, dim: int = 384, model_name: str | None = None) -> None:
        if dim < 32:
            raise ValueError("hashing embedding dimension must be >= 32")
        self.dim = dim
        self.model_name = model_name or f"hashing-{dim}"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature, weight in _features(text).items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * weight
        return _normalise(vector)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model_name, dim=self.dim, tokens=0)
        vectors = [self._vector(text) for text in texts]
        tokens = sum(max(1, len(text) // 4) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            dim=self.dim,
            tokens=tokens,
            provider=self.provider_name,
        )


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """POST {base_url}/embeddings with retries, batching and classified failures."""

    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = 1536,
        batch_size: int = 32,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise NanoMemoryError(
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                message="An API key is required for the openai_compatible embedding provider.",
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model_name = model
        self.dim = dim
        self.batch_size = max(1, batch_size)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model_name, dim=self.dim, tokens=0)
        vectors: list[list[float]] = []
        total_tokens = 0
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self.timeout_seconds
        ) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                payload = await self._post(client, batch)
                total_tokens += int(payload.get("usage", {}).get("total_tokens") or 0)
                rows = sorted(payload.get("data", []), key=lambda row: row.get("index", 0))
                if len(rows) != len(batch):
                    raise ProviderError(
                        ProviderErrorCategory.INVALID_RESPONSE,
                        "Embedding provider returned a different number of vectors than inputs.",
                        retryable=False,
                    )
                for row in rows:
                    vector = [float(value) for value in row.get("embedding", [])]
                    if not vector:
                        raise ProviderError(
                            ProviderErrorCategory.INVALID_RESPONSE,
                            "Embedding provider returned an empty vector.",
                            retryable=False,
                        )
                    if self.dim and len(vector) != self.dim:
                        raise NanoMemoryError(
                            code=ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                            message="Embedding dimension does not match the configured dimension.",
                            details={"expected": self.dim, "received": len(vector)},
                        )
                    vectors.append(_normalise(vector))
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            dim=len(vectors[0]),
            tokens=total_tokens,
            provider=self.provider_name,
        )

    async def _post(self, client: httpx.AsyncClient, batch: list[str]) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await client.post(
                    "/embeddings",
                    json={"model": self.model_name, "input": batch},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TimeoutException as exc:
                if attempt > self.max_retries:
                    raise ProviderError(
                        ProviderErrorCategory.TIMEOUT,
                        "Embedding provider timed out.",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(min(2.0, 0.25 * attempt))
                continue
            except httpx.HTTPError as exc:
                raise ProviderError(
                    ProviderErrorCategory.CONNECTION,
                    "Could not reach the embedding provider.",
                    retryable=True,
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt > self.max_retries:
                    category = (
                        ProviderErrorCategory.RATE_LIMIT
                        if response.status_code == 429
                        else ProviderErrorCategory.UNKNOWN
                    )
                    raise ProviderError(
                        category,
                        f"Embedding provider is unavailable (status {response.status_code}).",
                        retryable=True,
                    )
                await asyncio.sleep(min(2.0, 0.25 * attempt))
                continue
            if response.status_code == 401:
                raise ProviderError(
                    ProviderErrorCategory.AUTHENTICATION,
                    "Embedding provider rejected the credentials.",
                    retryable=False,
                )
            if response.status_code >= 400:
                raise ProviderError(
                    ProviderErrorCategory.INVALID_RESPONSE,
                    f"Embedding provider rejected the request (status {response.status_code}).",
                    retryable=False,
                )
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(
                    ProviderErrorCategory.INVALID_RESPONSE,
                    "Embedding provider returned a non-JSON body.",
                    retryable=False,
                ) from exc


class CachingEmbeddingProvider(EmbeddingProvider):
    """Process-local LRU cache keyed by (model, sha256(text)).

    Cache hits cost zero tokens; the returned EmbeddingResult reports the tokens actually
    spent, so cost accounting stays honest.
    """

    provider_name = "cached"

    def __init__(self, wrapped: EmbeddingProvider, *, max_entries: int = 2048) -> None:
        self.wrapped = wrapped
        self.model_name = wrapped.model_name
        self.dim = wrapped.dim
        self.provider_name = wrapped.provider_name
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max = max(1, max_entries)
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_name}\x00{text}".encode()).hexdigest()

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        missing: list[str] = []
        keys: list[str] = []
        for text in texts:
            key = self._key(text)
            keys.append(key)
            if key not in self._cache:
                missing.append(text)
        if missing:
            self.misses += len(missing)
            result = await self.wrapped.embed(missing)
            for text, vector in zip(missing, result.vectors, strict=True):
                key = self._key(text)
                self._cache[key] = vector
                self._cache.move_to_end(key)
                while len(self._cache) > self._max:
                    self._cache.popitem(last=False)
        else:
            result = EmbeddingResult(
                vectors=[], model=self.wrapped.model_name, dim=self.wrapped.dim, tokens=0
            )
        self.hits += len(texts) - len(missing)
        vectors = [self._cache[key] for key in keys]
        return EmbeddingResult(
            vectors=vectors,
            model=result.model,
            dim=result.dim or self.dim,
            tokens=result.tokens,
            provider=self.provider_name,
        )

    async def aclose(self) -> None:
        await self.wrapped.aclose()

    def cache_stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._cache)}


def build_embedding_provider(settings: object) -> EmbeddingProvider:
    """Build the configured provider; callers get a typed error when misconfigured."""
    provider = str(getattr(settings, "embedding_provider", "hashing"))
    dim = int(getattr(settings, "embedding_dimension", 384) or 384)
    if provider == "hashing":
        return CachingEmbeddingProvider(
            HashingEmbeddingProvider(dim=dim),
            max_entries=int(getattr(settings, "embedding_cache_entries", 2048) or 2048),
        )
    if provider == "openai_compatible":
        api_key = getattr(settings, "embedding_api_key", None) or getattr(settings, "api_key", None)
        secret = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        base_url = (
            getattr(settings, "embedding_base_url", None)
            or getattr(settings, "base_url", None)
            or "https://api.openai.com/v1"
        )
        model = getattr(settings, "embedding_model", None) or "text-embedding-3-small"
        return CachingEmbeddingProvider(
            OpenAICompatibleEmbeddingProvider(
                api_key=str(secret or ""),
                base_url=str(base_url),
                model=str(model),
                dim=dim,
                batch_size=int(getattr(settings, "embedding_batch_size", 32) or 32),
                timeout_seconds=float(getattr(settings, "embedding_timeout_seconds", 20.0) or 20),
                max_retries=int(getattr(settings, "embedding_max_retries", 2) or 0),
            ),
            max_entries=int(getattr(settings, "embedding_cache_entries", 2048) or 2048),
        )
    raise NanoMemoryError(
        code=ErrorCode.PROVIDER_NOT_CONFIGURED,
        message="Unknown embedding provider configured.",
        details={"provider": provider},
    )


__all__ = [
    "CachingEmbeddingProvider",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "build_embedding_provider",
]
