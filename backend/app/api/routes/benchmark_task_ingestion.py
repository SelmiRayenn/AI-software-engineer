from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud
from app.benchmark_tasks import (
    BenchmarkTaskCreationError,
    BenchmarkTaskValidationFailed,
    GitHubBenchmarkTaskCreator,
    InvalidTaskStatusTransition,
    mark_task_ready,
    to_agent_visible_task,
    validate_benchmark_task,
)
from app.db.session import get_db
from app.github import GitHubClientError, GitHubService
from app.schemas.benchmark_task import (
    AgentVisibleBenchmarkTaskRead,
    BenchmarkTaskFromGitHubRequest,
    BenchmarkTaskValidationResult,
)
from app.schemas.gold_patch import GoldPatchRead

router = APIRouter(tags=["benchmark tasks"])
evaluation_router = APIRouter(prefix="/evaluation", tags=["evaluation"])

DbSession = Annotated[Session, Depends(get_db)]


def get_github_service() -> GitHubService:
    return GitHubService()


GitHubServiceDep = Annotated[GitHubService, Depends(get_github_service)]


@router.post("/benchmark-tasks/from-github", response_model=AgentVisibleBenchmarkTaskRead)
def create_benchmark_task_from_github(
    request: BenchmarkTaskFromGitHubRequest,
    db: DbSession,
    github_service: GitHubServiceDep,
) -> AgentVisibleBenchmarkTaskRead:
    creator = GitHubBenchmarkTaskCreator(db=db, github_service=github_service)
    try:
        result = creator.create_from_github(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BenchmarkTaskCreationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail={
                "message": str(exc),
                "github_status_code": exc.status_code,
                "github_response": exc.response_json,
            },
        ) from exc

    return to_agent_visible_task(result.task, result.repository)


@router.post(
    "/benchmark-tasks/{task_id}/validate",
    response_model=BenchmarkTaskValidationResult,
)
def validate_benchmark_task_endpoint(
    task_id: UUID,
    db: DbSession,
) -> BenchmarkTaskValidationResult:
    task = crud.get_benchmark_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Benchmark task not found")
    return validate_benchmark_task(db, task)


@router.post(
    "/benchmark-tasks/{task_id}/mark-ready",
    response_model=AgentVisibleBenchmarkTaskRead,
)
def mark_benchmark_task_ready(
    task_id: UUID,
    db: DbSession,
) -> AgentVisibleBenchmarkTaskRead:
    task = crud.get_benchmark_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Benchmark task not found")

    try:
        task = mark_task_ready(db, task)
    except BenchmarkTaskValidationFailed as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.result.model_dump(mode="json"),
        ) from exc
    except InvalidTaskStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return to_agent_visible_task(task)


@evaluation_router.get(
    "/benchmark-tasks/{task_id}/gold-patch",
    response_model=GoldPatchRead,
)
def get_benchmark_task_gold_patch(task_id: UUID, db: DbSession) -> GoldPatchRead:
    gold_patch = crud.get_gold_patch_for_task(db, task_id)
    if gold_patch is None:
        raise HTTPException(status_code=404, detail="Gold patch not found")
    return gold_patch
