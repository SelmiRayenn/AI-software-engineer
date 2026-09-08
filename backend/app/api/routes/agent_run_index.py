from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.embeddings import EmbeddingsConfigError, EmbeddingsError
from app.repository_indexing import RepositoryIndexService
from app.repository_indexing.errors import (
    IndexLimitError,
    IndexNotFoundError,
    IndexRunNotFoundError,
    IndexSafetyError,
    IndexWorkspaceError,
    RepositoryIndexError,
)
from app.repository_indexing.semantic import EmbeddingBuildResult, RepositoryEmbeddingsService
from app.repository_indexing.service import SearchField
from app.schemas.repository_index import IndexedFileRead, RepositoryIndexRead

router = APIRouter(prefix="/agent-runs", tags=["repository indexing"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/{run_id}/index", response_model=RepositoryIndexRead)
def create_repository_index(run_id: UUID, db: DbSession):
    try:
        return RepositoryIndexService(db=db, agent_run_id=run_id).create_index()
    except RepositoryIndexError as exc:
        raise _http_error(exc) from exc


@router.post("/{run_id}/index/embeddings", response_model=EmbeddingBuildResult)
def create_repository_embeddings(run_id: UUID, db: DbSession):
    try:
        return RepositoryEmbeddingsService(db=db, agent_run_id=run_id).build()
    except RepositoryIndexError as exc:
        raise _http_error(exc) from exc
    except EmbeddingsConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmbeddingsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{run_id}/index/files", response_model=list[IndexedFileRead])
def list_indexed_files(
    run_id: UUID,
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return RepositoryIndexService(db=db, agent_run_id=run_id).list_files(
            limit=limit,
            offset=offset,
        )
    except RepositoryIndexError as exc:
        raise _http_error(exc) from exc


@router.get("/{run_id}/index/search", response_model=list[IndexedFileRead])
def search_indexed_files(
    run_id: UUID,
    db: DbSession,
    q: str = Query(min_length=1, max_length=200),
    field: SearchField = "all",
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    semantic: bool = False,
):
    try:
        return RepositoryIndexService(db=db, agent_run_id=run_id).search_files(
            q,
            field=field,
            limit=limit,
            offset=offset,
            semantic=semantic,
        )
    except RepositoryIndexError as exc:
        raise _http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _http_error(exc: RepositoryIndexError) -> HTTPException:
    if isinstance(exc, (IndexRunNotFoundError, IndexNotFoundError)):
        status_code = 404
    elif isinstance(exc, IndexWorkspaceError):
        status_code = 409
    elif isinstance(exc, (IndexSafetyError, IndexLimitError)):
        status_code = 422
    else:
        status_code = 500
    return HTTPException(status_code=status_code, detail=str(exc))
