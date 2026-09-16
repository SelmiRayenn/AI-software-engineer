from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.test_phases import TEST_PHASE_HIDDEN_EVAL
from app.core.trusted import require_trusted_operator
from app.db.session import get_db
from app.models import AgentRun, BenchmarkTask, HiddenEvalTest, TestResult
from app.schemas.hidden_eval_test import HiddenEvalTestCreate, HiddenEvalTestRead
from app.schemas.test_result import TestResultRead

router = APIRouter(tags=["hidden evaluation tests"])
DbSession = Annotated[Session, Depends(get_db)]
TrustedOperator = Annotated[None, Depends(require_trusted_operator)]


@router.post(
    "/benchmark-tasks/{task_id}/hidden-tests",
    response_model=HiddenEvalTestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_hidden_eval_test(
    task_id: UUID,
    request: HiddenEvalTestCreate,
    db: DbSession,
    _: TrustedOperator,
) -> HiddenEvalTestRead:
    if db.get(BenchmarkTask, task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark task not found."
        )
    hidden_test = HiddenEvalTest(benchmark_task_id=task_id, **request.model_dump())
    db.add(hidden_test)
    db.commit()
    db.refresh(hidden_test)
    return HiddenEvalTestRead.model_validate(hidden_test)


@router.get("/benchmark-tasks/{task_id}/hidden-tests", response_model=list[HiddenEvalTestRead])
def list_hidden_eval_tests(
    task_id: UUID,
    db: DbSession,
    _: TrustedOperator,
) -> list[HiddenEvalTestRead]:
    if db.get(BenchmarkTask, task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark task not found."
        )
    tests = list(
        db.scalars(
            select(HiddenEvalTest)
            .where(HiddenEvalTest.benchmark_task_id == task_id)
            .order_by(HiddenEvalTest.created_at.asc())
        )
    )
    return [HiddenEvalTestRead.model_validate(hidden_test) for hidden_test in tests]


@router.delete("/hidden-tests/{hidden_test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_hidden_eval_test(
    hidden_test_id: UUID,
    db: DbSession,
    _: TrustedOperator,
) -> None:
    hidden_test = db.get(HiddenEvalTest, hidden_test_id)
    if hidden_test is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Hidden evaluation test not found."
        )
    db.delete(hidden_test)
    db.commit()


@router.get("/agent-runs/{run_id}/tests/hidden-eval", response_model=list[TestResultRead])
def list_hidden_eval_results(run_id: UUID, db: DbSession, _: TrustedOperator) -> list[TestResult]:
    if db.get(AgentRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return list(
        db.scalars(
            select(TestResult)
            .where(TestResult.agent_run_id == run_id, TestResult.phase == TEST_PHASE_HIDDEN_EVAL)
            .order_by(TestResult.created_at, TestResult.id)
        )
    )
