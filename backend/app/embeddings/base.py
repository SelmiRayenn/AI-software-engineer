from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

MAX_EMBEDDING_TEXT_BYTES = 8000
MAX_EMBEDDING_REQUEST_BYTES = 128_000


class EmbeddingsError(RuntimeError):
    pass


class EmbeddingsConfigError(EmbeddingsError):
    pass


@dataclass(frozen=True)
class EmbeddingUsage:
    input_tokens: int | None = None
    estimated_cost: float | None = None
    latency_seconds: float = 0.0


class EmbeddingsProvider(ABC):
    provider_name: str
    model_name: str
    dimensions: int | None
    last_usage: EmbeddingUsage = EmbeddingUsage()

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input in input order; update last_usage for this call."""


def validate_texts(texts: list[str], batch_size: int) -> None:
    if not isinstance(texts, list) or not 1 <= len(texts) <= batch_size:
        raise EmbeddingsError(f"Embedding input must be a list of 1-{batch_size} texts.")
    sizes = []
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingsError("Embedding texts must be non-empty strings.")
        size = len(text.encode("utf-8"))
        if size > MAX_EMBEDDING_TEXT_BYTES:
            raise EmbeddingsError("Embedding text exceeds the 8000-byte input limit.")
        sizes.append(size)
    if sum(sizes) > MAX_EMBEDDING_REQUEST_BYTES:
        raise EmbeddingsError("Embedding request exceeds the 128000-byte input limit.")


def validate_vectors(vectors, count: int, dimensions: int | None = None) -> int:
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise EmbeddingsError("Embedding response must contain one vector per input.")
    expected = dimensions or (len(vectors[0]) if isinstance(vectors[0], list) else 0)
    if not 1 <= expected <= 4096:
        raise EmbeddingsError("Embedding vector dimensions must be between 1 and 4096.")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != expected:
            raise EmbeddingsError("Embedding vector dimensions do not match.")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise EmbeddingsError("Embedding vectors must contain finite numbers.")
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingsError("Embedding vectors must have a finite, nonzero norm.")
    return expected
