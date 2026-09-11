from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.failures import (
    AgentRunFailureNotFoundError,
    AgentRunNotFoundError,
    FailureClassificationService,
)
from app.schemas.failure import AgentRunFailureRead

router = APIRouter(prefix="/agent-runs", tags=["agent run failures"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}/failure", response_model=AgentRunFailureRead)
def get_agent_run_failure(run_id: UUID, db: DbSession) -> AgentRunFailureRead:
    try:
        failure = FailureClassificationService(db).get_or_classify(run_id)
    except (AgentRunNotFoundError, AgentRunFailureNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AgentRunFailureRead(
        id=failure.id,
        agent_run_id=failure.agent_run_id,
        category=failure.category,
        human_readable_summary=failure.human_readable_summary,
        source_event_id=failure.source_event_id,
        created_at=failure.created_at,
    )
