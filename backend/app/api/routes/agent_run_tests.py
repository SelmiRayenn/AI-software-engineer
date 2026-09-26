from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.test_phases import TEST_PHASE_HIDDEN_EVAL
from app.core.trusted import TRUSTED_OPERATOR_HEADER, verify_trusted_operator_token
from app.db.session import get_db
from app.models import AgentRun, TestResult
from app.schemas.targeted_tests import (
    TargetedTestSelectionRead,
    TargetedTestSelectionRequest,
)
from app.schemas.test_execution import TestExecutionRequest, TestExecutionResponse
from app.schemas.test_failure_analysis import TestFailureAnalysisRead
from app.schemas.test_result import TestResultRead
from app.targeted_tests import TargetedTestSelectionService
from app.test_execution import (
    TestExecutionError,
    TestExecutionSafetyError,
    TestExecutionService,
    TestExecutionWorkspaceError,
)
from app.test_failure_analysis import TestFailureAnalysisService

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
        .where(
            TestResult.agent_run_id == run_id,
            TestResult.phase != TEST_PHASE_HIDDEN_EVAL,
        )
        .order_by(TestResult.created_at.asc())
    )
    return list(db.scalars(statement).all())


@router.get(
    "/{run_id}/test-failure-analysis",
    response_model=list[TestFailureAnalysisRead],
)
def list_test_failure_analyses(
    run_id: UUID,
    db: DbSession = None,
) -> list[TestFailureAnalysisRead]:
    try:
        return TestFailureAnalysisService(db, run_id).list_for_run()
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{run_id}/tests/select-targeted",
    response_model=TargetedTestSelectionRead,
)
def select_targeted_tests(
    run_id: UUID,
    request: TargetedTestSelectionRequest | None = None,
    db: DbSession = None,
    operator_token: Annotated[str | None, Header(alias=TRUSTED_OPERATOR_HEADER)] = None,
) -> TargetedTestSelectionRead:
    request = request or TargetedTestSelectionRequest()
    if request.targeted_tests_trusted_gold_files:
        verify_trusted_operator_token(operator_token)
    try:
        service = TargetedTestSelectionService(db, run_id)
        config = service.stored_config()
        return service.select(
            max_commands=(
                request.targeted_tests_max_commands or config.targeted_tests_max_commands
            ),
            trusted_gold_files=request.targeted_tests_trusted_gold_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


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
