from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Any

from app.core.config import Settings, settings
from app.embeddings.base import (
    EmbeddingsConfigError,
    EmbeddingsError,
    EmbeddingsProvider,
    EmbeddingUsage,
    validate_texts,
    validate_vectors,
)

OPENAI_EMBEDDING_USD_PER_MILLION_TOKENS = {"text-embedding-3-small": 0.02}
OPENAI_DEFAULT_DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


class MockEmbeddingsProvider(EmbeddingsProvider):
    """Stable hashed token vectors for development, not a semantic language model."""

    provider_name = "mock"
    model_name = "mock-embeddings-v1"

    def __init__(self, *, app_settings: Settings = settings) -> None:
        self.dimensions = app_settings.embedding_vector_dimensions or 64
        self._batch_size = app_settings.embedding_batch_size

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.last_usage = EmbeddingUsage()
        validate_texts(texts, self._batch_size)
        started = time.perf_counter()
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in re.findall(r"\w+", text.casefold()) or [text]:
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
            norm = math.hypot(*vector)
            vectors.append([value / norm for value in vector])
        self.last_usage = EmbeddingUsage(0, 0.0, time.perf_counter() - started)
        return vectors


class OpenAIEmbeddingsProvider(EmbeddingsProvider):
    provider_name = "openai"

    def __init__(self, *, app_settings: Settings = settings, client: Any = None) -> None:
        self._settings = app_settings
        self._check_enabled()
        self.model_name = app_settings.openai_embedding_model
        self._requested_dimensions = app_settings.embedding_vector_dimensions
        self.dimensions = self._requested_dimensions or OPENAI_DEFAULT_DIMENSIONS.get(
            self.model_name
        )
        self._client = client

    def _check_enabled(self) -> None:
        if not self._settings.enable_real_embeddings:
            raise EmbeddingsConfigError(
                "OpenAI embeddings are disabled. Set ENABLE_REAL_EMBEDDINGS=true to enable them."
            )
        if not (self._settings.openai_api_key or "").strip():
            raise EmbeddingsConfigError("OPENAI_API_KEY is required for OpenAI embeddings.")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.last_usage = EmbeddingUsage()
        self._check_enabled()
        validate_texts(texts, self._settings.embedding_batch_size)
        started = time.perf_counter()
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._settings.openai_api_key, timeout=30.0, max_retries=0
            )
        request = {"model": self.model_name, "input": texts, "encoding_format": "float"}
        if self._requested_dimensions is not None:
            request["dimensions"] = self._requested_dimensions
        try:
            response = self._client.embeddings.create(**request)
        except Exception as exc:
            # Provider exceptions can contain request text, headers, or credentials.
            raise EmbeddingsError("OpenAI embedding request failed.") from exc
        try:
            data = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in data] != list(range(len(texts))):
                raise ValueError("Invalid embedding response indexes.")
            vectors = [item.embedding for item in data]
            validate_vectors(vectors, len(texts), self.dimensions)
            tokens = response.usage.prompt_tokens if response.usage else None
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError("Invalid embedding usage.")
        except (AttributeError, TypeError, ValueError) as exc:
            raise EmbeddingsError("OpenAI returned an invalid embedding response.") from exc
        price = OPENAI_EMBEDDING_USD_PER_MILLION_TOKENS.get(self.model_name)
        cost = tokens * price / 1_000_000 if tokens is not None and price is not None else None
        self.last_usage = EmbeddingUsage(tokens, cost, time.perf_counter() - started)
        return vectors


class LocalEmbeddingsProvider(EmbeddingsProvider):
    provider_name = "local"
    model_name = "local-embeddings"

    def __init__(self, *, app_settings: Settings = settings) -> None:
        self.dimensions = app_settings.embedding_vector_dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingsConfigError("LocalEmbeddingsProvider is not implemented yet.")


def create_embeddings_provider(*, app_settings: Settings = settings) -> EmbeddingsProvider:
    providers = {
        "mock": MockEmbeddingsProvider,
        "openai": OpenAIEmbeddingsProvider,
        "local": LocalEmbeddingsProvider,
    }
    return providers[app_settings.embeddings_provider](app_settings=app_settings)
