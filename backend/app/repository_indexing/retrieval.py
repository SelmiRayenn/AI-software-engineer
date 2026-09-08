from __future__ import annotations

import re
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, settings
from app.embeddings import EmbeddingsProvider
from app.models import IndexedFile, RepositoryIndex
from app.repository_indexing.errors import IndexNotFoundError
from app.repository_indexing.semantic import RepositoryEmbeddingsService
from app.repository_indexing.workspace import searchable_path

MAX_RETRIEVAL_QUERY_CHARS = 200
MAX_RETRIEVAL_RESULTS = 50
MAX_MATCHED_SYMBOLS = 20
MAX_MATCHED_SNIPPETS = 3
MAX_SNIPPET_CHARS = 240

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_TEST_INTENT_TERMS = {
    "assert",
    "assertion",
    "fail",
    "failed",
    "failing",
    "failure",
    "pytest",
    "test",
    "tests",
}
_SOURCE_INTENT_TERMS = {
    "class",
    "error",
    "exception",
    "function",
    "import",
    "module",
    "package",
    "traceback",
}


@dataclass(frozen=True)
class MatchedSnippet:
    line_number: int
    text: str


@dataclass(frozen=True)
class RelevantFile:
    file_path: str
    language: str
    file_kind: str
    matched_symbols: list[str]
    matched_snippets: list[MatchedSnippet]
    relevance_score: float
    size_bytes: int
    lexical_score: float = 0.0
    semantic_score: float | None = None


@dataclass(frozen=True)
class RelevantFilesResult:
    query: str
    files: list[RelevantFile]
    retrieval_mode: str = "lexical"
    fallback_reason: str | None = None


class RepositoryRetrievalService:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        embeddings_provider: EmbeddingsProvider | None = None,
        app_settings: Settings = settings,
    ) -> None:
        self._db = db
        self._run_id = agent_run_id
        self._embeddings_provider = embeddings_provider
        self._settings = app_settings

    def retrieve(
        self, query: str, *, limit: int = 10, semantic: bool = False
    ) -> RelevantFilesResult:
        normalized_query = _validate_query(query)
        _validate_limit(limit)
        index = self._db.scalar(
            select(RepositoryIndex).where(RepositoryIndex.agent_run_id == self._run_id)
        )
        if index is None:
            raise IndexNotFoundError(
                "Repository index not found. Create an index for this run first."
            )

        files = self._db.scalars(
            select(IndexedFile)
            .where(IndexedFile.repository_index_id == index.id)
            .options(selectinload(IndexedFile.symbols))
            .order_by(IndexedFile.file_path)
        ).all()
        result = self.rank_files(normalized_query, list(files), semantic=semantic)
        return replace(result, files=result.files[:limit])

    def rank_files(
        self,
        query: str,
        files: list[IndexedFile],
        *,
        semantic: bool = False,
    ) -> RelevantFilesResult:
        query = _validate_query(query)
        if not isinstance(semantic, bool):
            raise TypeError("Retrieval semantic option must be a boolean.")
        files = [file for file in files if searchable_path(file.file_path)]
        matches = [
            match
            for indexed_file in files
            if (match := _score_file(indexed_file, query)) is not None
        ]
        mode, fallback_reason = "lexical", None
        if semantic:
            result = RepositoryEmbeddingsService(
                db=self._db,
                agent_run_id=self._run_id,
                provider=self._embeddings_provider,
                app_settings=self._settings,
            ).search(query, files)
            fallback_reason = result.fallback_reason
            if fallback_reason is None:
                mode = "hybrid"
                by_path = {match.file_path: match for match in matches}
                for file in files:
                    hit = result.matches.get(file.id)
                    if hit is None:
                        continue
                    match = by_path.get(file.file_path) or RelevantFile(
                        file.file_path, file.language, file.file_type, [], [], 0.0, file.size_bytes
                    )
                    # The best matching chunk represents the file; positive cosine adds at most 10.
                    by_path[file.file_path] = replace(
                        match,
                        relevance_score=round(match.lexical_score + 10.0 * hit.score, 4),
                        semantic_score=round(hit.score, 6),
                        matched_symbols=list(
                            dict.fromkeys(match.matched_symbols + hit.chunk.symbol_names)
                        )[:MAX_MATCHED_SYMBOLS],
                        matched_snippets=(
                            match.matched_snippets
                            or [
                                MatchedSnippet(
                                    hit.chunk.start_line,
                                    hit.chunk.content.strip()[:MAX_SNIPPET_CHARS],
                                )
                            ]
                        ),
                    )
                matches = list(by_path.values())
        matches.sort(key=lambda match: (-match.relevance_score, match.file_path))
        return RelevantFilesResult(query, matches, mode, fallback_reason)


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise TypeError("Retrieval query must be a string.")
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("Retrieval query must not be empty.")
    if len(query) > MAX_RETRIEVAL_QUERY_CHARS:
        raise ValueError(f"Retrieval query must not exceed {MAX_RETRIEVAL_QUERY_CHARS} characters.")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("Retrieval limit must be an integer.")
    if not 1 <= limit <= MAX_RETRIEVAL_RESULTS:
        raise ValueError(f"Retrieval limit must be between 1 and {MAX_RETRIEVAL_RESULTS}.")


def _score_file(indexed_file: IndexedFile, query: str) -> RelevantFile | None:
    query_text = query.casefold()
    tokens = _query_tokens(query)
    path_text = indexed_file.file_path.casefold()
    content_text = indexed_file.content.casefold()

    path_score = 8.0 if query_text in path_text else 0.0
    path_score += sum(2.0 for token in tokens if token in path_text)

    matched_symbols: list[str] = []
    symbol_score = 0.0
    for symbol in indexed_file.symbols:
        symbol_text = symbol.name.casefold()
        if query_text not in symbol_text and not any(token in symbol_text for token in tokens):
            continue
        matched_symbols.append(symbol.name)
        symbol_score += 8.0 if symbol_text in tokens else 5.0
        if len(matched_symbols) >= MAX_MATCHED_SYMBOLS:
            break

    phrase_match = len(query_text) >= 3 and query_text in content_text
    token_hits = sum(min(content_text.count(token), 4) for token in tokens)
    text_score = (3.0 if phrase_match else 0.0) + min(token_hits * 0.5, 6.0)
    base_score = path_score + symbol_score + text_score
    if base_score <= 0:
        return None

    token_set = set(tokens)
    boost = 0.0
    if indexed_file.file_type == "test" and token_set & _TEST_INTENT_TERMS:
        boost += 3.0
    if indexed_file.file_type == "source" and token_set & _SOURCE_INTENT_TERMS:
        boost += 2.0

    return RelevantFile(
        file_path=indexed_file.file_path,
        language=indexed_file.language,
        file_kind=indexed_file.file_type,
        matched_symbols=matched_symbols,
        matched_snippets=_matched_snippets(indexed_file.content, query_text, tokens),
        relevance_score=round(base_score + boost, 4),
        size_bytes=indexed_file.size_bytes,
        lexical_score=round(base_score + boost, 4),
    )


def _query_tokens(query: str) -> list[str]:
    all_tokens = list(
        dict.fromkeys(match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(query))
    )
    meaningful = [token for token in all_tokens if len(token) > 1 and token not in _STOP_WORDS]
    return meaningful or all_tokens


def _matched_snippets(content: str, query: str, tokens: list[str]) -> list[MatchedSnippet]:
    snippets: list[MatchedSnippet] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        normalized_line = line.casefold()
        if query not in normalized_line and not any(token in normalized_line for token in tokens):
            continue
        text = line.strip()
        if len(text) > MAX_SNIPPET_CHARS:
            text = f"{text[: MAX_SNIPPET_CHARS - 3]}..."
        snippets.append(MatchedSnippet(line_number=line_number, text=text))
        if len(snippets) >= MAX_MATCHED_SNIPPETS:
            break
    return snippets
