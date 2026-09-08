from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun


class RepositoryIndex(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "repository_indexes"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    skipped_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="repository_index")
    files: Mapped[list["IndexedFile"]] = relationship(
        back_populates="repository_index",
        cascade="all, delete-orphan",
        order_by="IndexedFile.file_path",
    )


class IndexedFile(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "indexed_files"
    __table_args__ = (
        UniqueConstraint("repository_index_id", "file_path", name="uq_indexed_files_index_path"),
    )

    repository_index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("repository_indexes.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    extension: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(100), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    preview: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    imports: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    python_parse_error: Mapped[bool] = mapped_column(default=False, nullable=False)

    repository_index: Mapped["RepositoryIndex"] = relationship(back_populates="files")
    symbols: Mapped[list["IndexedSymbol"]] = relationship(
        back_populates="indexed_file",
        cascade="all, delete-orphan",
        order_by="(IndexedSymbol.line_number, IndexedSymbol.name)",
    )
    chunks: Mapped[list["IndexedChunk"]] = relationship(
        back_populates="indexed_file", cascade="all, delete-orphan", order_by="IndexedChunk.ordinal"
    )


class IndexedSymbol(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "indexed_symbols"

    indexed_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("indexed_files.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    indexed_file: Mapped["IndexedFile"] = relationship(back_populates="symbols")


class IndexedChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "indexed_chunks"
    __table_args__ = (
        UniqueConstraint("indexed_file_id", "ordinal", name="uq_indexed_chunks_file_ordinal"),
    )

    indexed_file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("indexed_files.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    indexed_file: Mapped["IndexedFile"] = relationship(back_populates="chunks")
    embedding: Mapped["ChunkEmbedding | None"] = relationship(
        back_populates="chunk", cascade="all, delete-orphan", uselist=False
    )


class ChunkEmbedding(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "chunk_embeddings"

    indexed_chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("indexed_chunks.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    input_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk: Mapped["IndexedChunk"] = relationship(back_populates="embedding")
