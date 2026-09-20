from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud
from app.benchmark_tasks import to_agent_visible_task
from app.core.task_metadata import normalize_difficulty, normalize_tags
from app.db.session import get_db
from app.schemas.benchmark_task import (
    AgentVisibleBenchmarkTaskRead,
    BenchmarkTaskCreate,
    BenchmarkTaskMetadataUpdate,
)

router = APIRouter(prefix="/benchmark-tasks", tags=["benchmark tasks"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[AgentVisibleBenchmarkTaskRead])
def list_benchmark_tasks(
    repository_id: UUID | None = None,
    difficulty: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: DbSession = None,
) -> list[AgentVisibleBenchmarkTaskRead]:
    try:
        normalized_difficulty = normalize_difficulty(difficulty) if difficulty is not None else None
        normalized_tag = normalize_tags([tag])[0] if tag is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tasks = crud.list_benchmark_tasks(
        db=db,
        repository_id=repository_id,
        difficulty=normalized_difficulty,
        tag=normalized_tag,
        skip=skip,
        limit=limit,
    )
    return [to_agent_visible_task(task) for task in tasks]


@router.get("/tags", response_model=list[str])
def list_benchmark_task_tags(db: DbSession = None) -> list[str]:
    tasks = crud.list_benchmark_tasks(db=db, limit=500)
    return sorted({tag for task in tasks for tag in task.tags})


@router.patch("/{task_id}/metadata", response_model=AgentVisibleBenchmarkTaskRead)
def update_benchmark_task_metadata(
    task_id: UUID,
    metadata: BenchmarkTaskMetadataUpdate,
    db: DbSession = None,
) -> AgentVisibleBenchmarkTaskRead:
    task = crud.get_benchmark_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Benchmark task not found")
    updates = metadata.model_dump(exclude_unset=True)
    if "difficulty" in updates:
        task.difficulty = updates["difficulty"]
    if "tags" in updates:
        task.tags = updates["tags"]
    db.commit()
    db.refresh(task)
    return to_agent_visible_task(task)


@router.post("", response_model=AgentVisibleBenchmarkTaskRead, status_code=status.HTTP_201_CREATED)
def create_benchmark_task(
    task_in: BenchmarkTaskCreate,
    db: DbSession = None,
) -> AgentVisibleBenchmarkTaskRead:
    repository = crud.get_repository(db, task_in.repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    task = crud.create_benchmark_task(db=db, task_in=task_in)
    return to_agent_visible_task(task, repository)
