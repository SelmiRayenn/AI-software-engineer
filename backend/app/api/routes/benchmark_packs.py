from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.benchmark_packs import (
    BenchmarkPackConflict,
    BenchmarkPackNotFound,
    BenchmarkPackService,
    BenchmarkTaskNotFound,
)
from app.db.session import get_db
from app.schemas.benchmark_pack import (
    BenchmarkPackCreate,
    BenchmarkPackDetail,
    BenchmarkPackRead,
    BenchmarkPackTaskCreate,
    BenchmarkPackTaskRead,
)

router = APIRouter(prefix="/benchmark-packs", tags=["benchmark packs"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("", response_model=BenchmarkPackRead, status_code=status.HTTP_201_CREATED)
def create_benchmark_pack(request: BenchmarkPackCreate, db: DbSession) -> BenchmarkPackRead:
    try:
        return BenchmarkPackService(db).create_pack(request)
    except BenchmarkPackConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[BenchmarkPackRead])
def list_benchmark_packs(
    db: DbSession,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[BenchmarkPackRead]:
    return BenchmarkPackService(db).list_packs(skip=skip, limit=limit)


@router.get("/{pack_id}", response_model=BenchmarkPackDetail)
def get_benchmark_pack(pack_id: UUID, db: DbSession) -> BenchmarkPackDetail:
    try:
        return BenchmarkPackService(db).get_pack(pack_id)
    except BenchmarkPackNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{pack_id}/tasks",
    response_model=BenchmarkPackTaskRead,
    status_code=status.HTTP_201_CREATED,
)
def add_benchmark_pack_task(
    pack_id: UUID, request: BenchmarkPackTaskCreate, db: DbSession
) -> BenchmarkPackTaskRead:
    try:
        return BenchmarkPackService(db).add_task(pack_id, request)
    except (BenchmarkPackNotFound, BenchmarkTaskNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BenchmarkPackConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{pack_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_benchmark_pack_task(pack_id: UUID, task_id: UUID, db: DbSession) -> None:
    try:
        BenchmarkPackService(db).remove_task(pack_id, task_id)
    except (BenchmarkPackNotFound, BenchmarkTaskNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
