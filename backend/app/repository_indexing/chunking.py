from __future__ import annotations

import hashlib

from app.core.config import Settings, settings
from app.models import IndexedChunk, IndexedFile
from app.repository_indexing.workspace import searchable_path

MAX_CHUNK_FILE_BYTES = 256_000
MAX_INDEX_CHUNKS = 10_000


def file_can_be_embedded(file: IndexedFile) -> bool:
    return (
        searchable_path(file.file_path)
        and file.size_bytes <= MAX_CHUNK_FILE_BYTES
        and len(file.content.encode("utf-8")) <= MAX_CHUNK_FILE_BYTES
        and not any(ord(char) < 32 and char not in "\t\n\f\r" for char in file.content)
    )


def chunk_file(file: IndexedFile, *, app_settings: Settings = settings) -> list[IndexedChunk]:
    if not file_can_be_embedded(file):
        return []
    size = app_settings.embedding_chunk_size_chars
    overlap = app_settings.embedding_chunk_overlap_chars
    chunks = []
    start = 0
    while start < len(file.content):
        end = min(start + size, len(file.content))
        # Prefer a line boundary, but split long source lines to preserve the hard bound.
        if end < len(file.content):
            newline = file.content.rfind("\n", start + max(size // 2, overlap + 1), end)
            if newline != -1:
                end = newline + 1
        content = file.content[start:end]
        start_line = file.content.count("\n", 0, start) + 1
        end_line = file.content.count("\n", 0, end - 1) + 1
        if content.strip():
            names = [
                symbol.name[:64]
                for symbol in file.symbols
                if symbol.line_number <= end_line and symbol.end_line_number >= start_line
            ][:20]
            chunks.append(
                IndexedChunk(
                    ordinal=len(chunks),
                    start_line=start_line,
                    end_line=end_line,
                    content=content,
                    symbol_names=names,
                    checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
        if end == len(file.content):
            break
        start = end - overlap
    return chunks


def embedding_text(file: IndexedFile, chunk: IndexedChunk) -> str:
    path = _byte_prefix(file.file_path, 1024)
    symbols = _byte_prefix(", ".join(chunk.symbol_names), 256)
    return f"File: {path}\nSymbols: {symbols}\n\n{chunk.content}"


def _byte_prefix(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
