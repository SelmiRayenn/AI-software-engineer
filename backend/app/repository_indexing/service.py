from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import AgentRun, IndexedFile, IndexedSymbol, RepositoryIndex
from app.repository_indexing.errors import (
    IndexLimitError,
    IndexNotFoundError,
    IndexRunNotFoundError,
    IndexSafetyError,
    IndexWorkspaceError,
    SkippedFile,
)
from app.repository_indexing.extraction import extract_file
from app.repository_indexing.retrieval import RepositoryRetrievalService
from app.repository_indexing.workspace import IndexWorkspace
from app.sandbox.workspace import SandboxWorkspaceManager

SearchField = Literal["all", "path", "text", "symbol"]
INDEX_VERSION = 1


@dataclass(frozen=True)
class IndexLimits:
    max_file_bytes: int = 256_000
    max_total_bytes: int = 20_000_000
    max_files: int = 10_000
    max_entries: int = 50_000

    def __post_init__(self) -> None:
        if min(self.max_file_bytes, self.max_total_bytes, self.max_files, self.max_entries) < 1:
            raise ValueError("Index limits must be positive.")


class RepositoryIndexService:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        workspace_manager: SandboxWorkspaceManager | None = None,
        limits: IndexLimits | None = None,
    ) -> None:
        self._db = db
        self._run_id = agent_run_id
        self._manager = workspace_manager or SandboxWorkspaceManager()
        self._limits = limits or IndexLimits()
        if db.get(AgentRun, agent_run_id) is None:
            raise IndexRunNotFoundError("Agent run not found.")

    def create_index(self) -> RepositoryIndex:
        # Lock the run row to serialize snapshot replacement on PostgreSQL.
        run = self._db.scalar(select(AgentRun).where(AgentRun.id == self._run_id).with_for_update())
        if run is None:
            raise IndexRunNotFoundError("Agent run not found.")
        try:
            workspace = IndexWorkspace(
                root=self._manager.workspace_root,
                workspace_id=run.workspace_id,
                workspace_path=run.workspace_path,
            )
            skipped: Counter[str] = Counter()
            files: list[IndexedFile] = []
            total_bytes = 0
            for path in workspace.files(skipped, max_entries=self._limits.max_entries):
                try:
                    data = workspace.read_file(path, self._limits.max_file_bytes)
                    file = extract_file(path, data)
                except SkippedFile as exc:
                    skipped[str(exc)] += 1
                    continue
                except IndexSafetyError:
                    skipped["unsafe_entries"] += 1
                    continue
                total_bytes += file.size_bytes
                if (
                    len(files) >= self._limits.max_files
                    or total_bytes > self._limits.max_total_bytes
                ):
                    raise IndexLimitError(
                        "Repository exceeds the index file count or total byte limit."
                    )
                files.append(file)
            files.sort(key=lambda file: file.file_path)
            manifest = [(file.file_path, file.checksum) for file in files]
            checksum = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            index = self._db.scalar(
                select(RepositoryIndex).where(RepositoryIndex.agent_run_id == self._run_id)
            )
            if index is None:
                index = RepositoryIndex(agent_run_id=self._run_id)
                self._db.add(index)
            else:
                index.files.clear()
                self._db.flush()
            index.workspace_id = run.workspace_id
            index.index_version = INDEX_VERSION
            index.indexed_at = datetime.now(UTC)
            index.file_count = len(files)
            index.total_size_bytes = total_bytes
            index.checksum = checksum
            index.skipped_counts = dict(sorted(skipped.items()))
            index.files = files
            self._db.commit()
            self._db.refresh(index)
            return index
        except OSError as exc:
            self._db.rollback()
            raise IndexWorkspaceError(
                "Workspace could not be read; retry on a quiet checkout."
            ) from exc
        except Exception:
            self._db.rollback()
            raise

    def list_files(self, *, limit: int = 100, offset: int = 0) -> list[IndexedFile]:
        _validate_page(limit, offset)
        return list(self._db.scalars(self._files_query().limit(limit).offset(offset)).all())

    def search_files(
        self,
        query: str,
        *,
        field: SearchField = "all",
        limit: int = 100,
        offset: int = 0,
        semantic: bool = False,
    ) -> list[IndexedFile]:
        query = query.strip()
        if not query or len(query) > 200:
            raise ValueError("Search query must contain between 1 and 200 characters.")
        if field not in {"all", "path", "text", "symbol"}:
            raise ValueError("Search field must be all, path, text, or symbol.")
        _validate_page(limit, offset)
        if semantic:
            if field != "all":
                raise ValueError("Semantic search requires field=all.")
            files = list(self._db.scalars(self._files_query()))
            result = RepositoryRetrievalService(db=self._db, agent_run_id=self._run_id).rank_files(
                query,
                files,
                semantic=True,
            )
            by_path = {file.file_path: file for file in files}
            return [by_path[match.file_path] for match in result.files[offset : offset + limit]]
        filters = {
            "path": IndexedFile.file_path.icontains(query, autoescape=True),
            "text": IndexedFile.content.icontains(query, autoescape=True),
            "symbol": IndexedFile.symbols.any(IndexedSymbol.name.icontains(query, autoescape=True)),
        }
        condition = or_(*filters.values()) if field == "all" else filters[field]
        statement = self._files_query().where(condition).limit(limit).offset(offset)
        return list(self._db.scalars(statement).all())

    def _files_query(self):
        index = self._db.scalar(
            select(RepositoryIndex).where(RepositoryIndex.agent_run_id == self._run_id)
        )
        if index is None:
            raise IndexNotFoundError(
                "Repository index not found. Create an index for this run first."
            )
        return (
            select(IndexedFile)
            .where(IndexedFile.repository_index_id == index.id)
            .options(selectinload(IndexedFile.symbols))
            .order_by(IndexedFile.file_path)
        )


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Index queries require limit 1-200 and a non-negative offset.")
