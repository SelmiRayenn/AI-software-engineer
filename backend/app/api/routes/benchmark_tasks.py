from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud
from app.benchmark_tasks import to_agent_visible_task
from app.db.session import get_db
from app.schemas.benchmark_task import AgentVisibleBenchmarkTaskRead, BenchmarkTaskCreate

router = APIRouter(prefix="/benchmark-tasks", tags=["benchmark tasks"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[AgentVisibleBenchmarkTaskRead])
def list_benchmark_tasks(
    repository_id: UUID | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: DbSession = None,
) -> list[AgentVisibleBenchmarkTaskRead]:
    tasks = crud.list_benchmark_tasks(
        db=db,
        repository_id=repository_id,
        skip=skip,
        limit=limit,
    )
    return [to_agent_visible_task(task) for task in tasks]


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
