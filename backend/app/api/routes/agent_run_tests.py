from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AgentRun, TestResult
from app.schemas.test_execution import TestExecutionRequest, TestExecutionResponse
from app.schemas.test_result import TestResultRead
from app.test_execution import (
    TestExecutionError,
    TestExecutionSafetyError,
    TestExecutionService,
    TestExecutionWorkspaceError,
)

router = APIRouter(prefix="/agent-runs", tags=["agent run tests"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/{run_id}/tests/baseline", response_model=TestExecutionResponse)
def run_baseline_tests(
    run_id: UUID,
    request: TestExecutionRequest | None = None,
    db: DbSession = None,
) -> TestExecutionResponse:
    service = _test_execution_service(db, run_id, request)
    try:
        result = service.run_baseline_tests(run_setup=True)
    except TestExecutionError as exc:
        raise _http_error(exc) from exc
    return _response(result)


@router.post("/{run_id}/tests/post-patch", response_model=TestExecutionResponse)
def run_post_patch_tests(
    run_id: UUID,
    request: TestExecutionRequest | None = None,
    db: DbSession = None,
) -> TestExecutionResponse:
    service = _test_execution_service(db, run_id, request)
    try:
        result = service.run_post_patch_tests()
    except TestExecutionError as exc:
        raise _http_error(exc) from exc
    return _response(result)


@router.get("/{run_id}/tests", response_model=list[TestResultRead])
def list_agent_run_tests(run_id: UUID, db: DbSession = None) -> list[TestResultRead]:
    if db.get(AgentRun, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found.")
    statement = (
        select(TestResult)
        .where(TestResult.agent_run_id == run_id)
        .order_by(TestResult.created_at.asc())
    )
    return list(db.scalars(statement).all())


def _test_execution_service(
    db: Session,
    run_id: UUID,
    request: TestExecutionRequest | None,
) -> TestExecutionService:
    try:
        return TestExecutionService(
            db=db,
            agent_run_id=run_id,
            command_timeout_seconds=(
                request.command_timeout_seconds if request is not None else None
            ),
        )
    except TestExecutionError as exc:
        raise _http_error(exc) from exc


def _response(result) -> TestExecutionResponse:
    return TestExecutionResponse(
        agent_run_id=result.agent_run_id,
        phase=result.phase,
        passed=result.passed,
        setup_results=result.setup_results,
        test_results=result.test_results,
        patch_status=result.patch_status,
        error_message=result.error_message,
    )


def _http_error(exc: TestExecutionError) -> HTTPException:
    if isinstance(exc, TestExecutionWorkspaceError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, TestExecutionSafetyError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
