from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app import crud
from app.db.session import get_db
from app.schemas.repository import RepositoryCreate, RepositoryRead

router = APIRouter(prefix="/repositories", tags=["repositories"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[RepositoryRead])
def list_repositories(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: DbSession = None,
) -> list[RepositoryRead]:
    return crud.list_repositories(db=db, skip=skip, limit=limit)


@router.post("", response_model=RepositoryRead, status_code=status.HTTP_201_CREATED)
def create_repository(
    repository_in: RepositoryCreate,
    db: DbSession = None,
) -> RepositoryRead:
    return crud.create_repository(db=db, repository_in=repository_in)
