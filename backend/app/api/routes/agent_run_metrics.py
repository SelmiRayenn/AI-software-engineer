from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.evaluation import EvaluationError, EvaluationRunNotCompleteError, EvaluationService
from app.schemas.evaluation_metric import EvaluationMetricRead

router = APIRouter(prefix="/agent-runs", tags=["agent run metrics"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/{run_id}/evaluate", response_model=EvaluationMetricRead)
def evaluate_agent_run(run_id: UUID, db: DbSession = None) -> EvaluationMetricRead:
    try:
        return EvaluationService(db=db, agent_run_id=run_id).evaluate()
    except EvaluationError as exc:
        raise _http_error(exc) from exc


@router.get("/{run_id}/metrics", response_model=EvaluationMetricRead)
def get_agent_run_metrics(run_id: UUID, db: DbSession = None) -> EvaluationMetricRead:
    try:
        metric = EvaluationService(db=db, agent_run_id=run_id).get_metrics()
    except EvaluationError as exc:
        raise _http_error(exc) from exc
    if metric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evaluation metrics not found.",
        )
    return metric


def _http_error(exc: EvaluationError) -> HTTPException:
    if isinstance(exc, EvaluationRunNotCompleteError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
