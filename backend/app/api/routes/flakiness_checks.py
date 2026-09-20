from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routes.agent_run_orchestration import DbSession
from app.flakiness import FlakinessCheckError, FlakinessCheckService, FlakinessCheckTaskNotFound
from app.sandbox import DockerSandboxRunner
from app.schemas.flakiness import FlakinessCheckRead, FlakinessCheckRequest

router = APIRouter(prefix="/benchmark-tasks", tags=["benchmark task flakiness"])


def get_flakiness_runner() -> DockerSandboxRunner:
    return DockerSandboxRunner()


FlakinessRunnerDep = Annotated[DockerSandboxRunner, Depends(get_flakiness_runner)]


@router.post(
    "/{task_id}/flakiness-check",
    response_model=FlakinessCheckRead,
    status_code=status.HTTP_201_CREATED,
)
def run_flakiness_check(
    task_id: UUID,
    request: FlakinessCheckRequest,
    db: DbSession,
    runner: FlakinessRunnerDep,
) -> FlakinessCheckRead:
    try:
        return FlakinessCheckService(db, runner=runner).run(task_id, request)
    except FlakinessCheckTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FlakinessCheckError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{task_id}/flakiness-checks", response_model=list[FlakinessCheckRead])
def list_flakiness_checks(
    task_id: UUID,
    db: DbSession,
    runner: FlakinessRunnerDep,
) -> list[FlakinessCheckRead]:
    try:
        return FlakinessCheckService(db, runner=runner).list_for_task(task_id)
    except FlakinessCheckTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
