from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, settings
from app.embeddings import EmbeddingsError, EmbeddingsProvider, create_embeddings_provider
from app.embeddings.base import MAX_EMBEDDING_REQUEST_BYTES, validate_texts, validate_vectors
from app.models import (
    AgentEvent,
    AgentRun,
    ChunkEmbedding,
    IndexedChunk,
    IndexedFile,
    RepositoryIndex,
)
from app.repository_indexing.chunking import (
    MAX_INDEX_CHUNKS,
    chunk_file,
    embedding_text,
    file_can_be_embedded,
)
from app.repository_indexing.errors import (
    IndexLimitError,
    IndexNotFoundError,
    IndexRunNotFoundError,
)

MAX_VECTOR_VALUES = 2_000_000


@dataclass(frozen=True)
class EmbeddingBuildResult:
    repository_index_id: UUID
    chunk_count: int
    provider_name: str
    model_name: str
    dimensions: int | None
    input_tokens: int | None
    estimated_cost: float | None


@dataclass(frozen=True)
class SemanticMatch:
    chunk: IndexedChunk
    score: float


@dataclass(frozen=True)
class SemanticResult:
    matches: dict[UUID, SemanticMatch]
    fallback_reason: str | None = None


class RepositoryEmbeddingsService:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        provider: EmbeddingsProvider | None = None,
        app_settings: Settings = settings,
    ) -> None:
        self._db = db
        self._run_id = agent_run_id
        self._provider = provider
        self._settings = app_settings

    def build(self) -> EmbeddingBuildResult:
        # Share the run lock with index rebuilds, so vectors belong to one consistent snapshot.
        try:
            run = self._db.scalar(
                select(AgentRun).where(AgentRun.id == self._run_id).with_for_update()
            )
            if run is None:
                raise IndexRunNotFoundError("Agent run not found.")
            index = self._db.scalar(
                select(RepositoryIndex).where(RepositoryIndex.agent_run_id == self._run_id)
            )
            if index is None:
                raise IndexNotFoundError("Create a repository index before building embeddings.")
            provider = self._provider or create_embeddings_provider(app_settings=self._settings)
            files = list(
                self._db.scalars(
                    select(IndexedFile)
                    .where(IndexedFile.repository_index_id == index.id)
                    .options(selectinload(IndexedFile.symbols), selectinload(IndexedFile.chunks))
                    .order_by(IndexedFile.file_path)
                )
            )
            plan = []
            for file in files:
                for chunk in chunk_file(file, app_settings=self._settings):
                    plan.append((file, chunk, embedding_text(file, chunk)))
                    if len(plan) > MAX_INDEX_CHUNKS:
                        raise IndexLimitError("Repository exceeds the 10000-chunk embedding limit.")
            dimensions = provider.dimensions
            if dimensions is not None and len(plan) * dimensions > MAX_VECTOR_VALUES:
                raise IndexLimitError("Repository exceeds the embedding vector storage limit.")

            tokens: int | None = 0
            cost: float | None = 0.0
            rows = []
            for batch in _batches(plan, self._settings.embedding_batch_size):
                texts = [item[2] for item in batch]
                validate_texts(texts, self._settings.embedding_batch_size)
                vectors = provider.embed_texts(texts)
                dimensions = validate_vectors(vectors, len(batch), dimensions)
                if len(plan) * dimensions > MAX_VECTOR_VALUES:
                    raise IndexLimitError("Repository exceeds the embedding vector storage limit.")
                usage = provider.last_usage
                tokens = (
                    tokens + usage.input_tokens
                    if tokens is not None and usage.input_tokens is not None
                    else None
                )
                cost = (
                    cost + usage.estimated_cost
                    if cost is not None and usage.estimated_cost is not None
                    else None
                )
                self._record_usage(provider, "build")
                for (file, chunk, text), vector in zip(batch, vectors, strict=True):
                    chunk.embedding = ChunkEmbedding(
                        provider_name=provider.provider_name,
                        model_name=provider.model_name,
                        dimensions=dimensions,
                        vector=vector,
                        input_checksum=_checksum(text),
                    )
                    rows.append((file, chunk))

            # Publish all chunks together; a provider failure leaves the old snapshot intact.
            for file in files:
                file.chunks.clear()
            self._db.flush()
            for file, chunk in rows:
                file.chunks.append(chunk)
            result = EmbeddingBuildResult(
                index.id,
                len(rows),
                provider.provider_name,
                provider.model_name,
                dimensions,
                tokens,
                cost,
            )
            self._db.add(
                AgentEvent(
                    agent_run_id=self._run_id,
                    event_type="repository_embeddings_created",
                    payload_json={**asdict(result), "repository_index_id": str(index.id)},
                )
            )
            self._db.commit()
            return result
        except Exception:
            self._db.rollback()
            raise

    def search(self, query: str, files: list[IndexedFile]) -> SemanticResult:
        allowed_files = {file.id: file for file in files if file_can_be_embedded(file)}
        rows = list(
            self._db.scalars(
                select(IndexedChunk)
                .join(IndexedFile)
                .join(RepositoryIndex)
                .where(RepositoryIndex.agent_run_id == self._run_id)
                .options(selectinload(IndexedChunk.embedding))
                .order_by(IndexedFile.file_path, IndexedChunk.ordinal)
                .limit(MAX_INDEX_CHUNKS + 1)
            )
        )
        rows = [
            chunk for chunk in rows if chunk.indexed_file_id in allowed_files and chunk.embedding
        ]
        if not rows:
            return SemanticResult({}, "embeddings_unavailable")
        if len(rows) > MAX_INDEX_CHUNKS:
            return SemanticResult({}, "embedding_limit_exceeded")
        try:
            provider = self._provider or create_embeddings_provider(app_settings=self._settings)
            compatible = []
            vector_values = 0
            for chunk in rows:
                embedding = chunk.embedding
                if (
                    embedding.provider_name != provider.provider_name
                    or embedding.model_name != provider.model_name
                    or (
                        provider.dimensions is not None
                        and embedding.dimensions != provider.dimensions
                    )
                    or _checksum(chunk.content) != chunk.checksum
                    or _checksum(embedding_text(allowed_files[chunk.indexed_file_id], chunk))
                    != embedding.input_checksum
                ):
                    continue
                try:
                    validate_vectors([embedding.vector], 1, embedding.dimensions)
                except EmbeddingsError:
                    continue
                vector_values += embedding.dimensions
                if vector_values > MAX_VECTOR_VALUES:
                    return SemanticResult({}, "embedding_limit_exceeded")
                compatible.append(chunk)
            if not compatible:
                return SemanticResult({}, "compatible_embeddings_unavailable")
            validate_texts([query], self._settings.embedding_batch_size)
            vectors = provider.embed_texts([query])
            dimensions = validate_vectors(vectors, 1, provider.dimensions)
            self._record_usage(provider, "query")
            self._db.commit()
        except EmbeddingsError:
            return SemanticResult({}, "embedding_provider_unavailable")

        if not any(chunk.embedding.dimensions == dimensions for chunk in compatible):
            return SemanticResult({}, "compatible_embeddings_unavailable")

        matches = {}
        for chunk in compatible:
            vector = chunk.embedding.vector
            if chunk.embedding.dimensions != dimensions:
                continue
            score = _cosine(vectors[0], vector)
            previous = matches.get(chunk.indexed_file_id)
            if score > 0 and (previous is None or score > previous.score):
                matches[chunk.indexed_file_id] = SemanticMatch(chunk, score)
        return SemanticResult(matches)

    def _record_usage(self, provider: EmbeddingsProvider, purpose: str) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=self._run_id,
                event_type="embedding_call_completed",
                payload_json={
                    "purpose": purpose,
                    "provider_name": provider.provider_name,
                    "model_name": provider.model_name,
                    **asdict(provider.last_usage),
                },
            )
        )


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _batches(plan: list, batch_size: int):
    batch = []
    size = 0
    for item in plan:
        item_size = len(item[2].encode("utf-8"))
        if batch and (len(batch) >= batch_size or size + item_size > MAX_EMBEDDING_REQUEST_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch


def _cosine(left: list[float], right: list[float]) -> float:
    left_norm, right_norm = math.hypot(*left), math.hypot(*right)
    return max(
        -1.0,
        min(1.0, sum((a / left_norm) * (b / right_norm) for a, b in zip(left, right, strict=True))),
    )
