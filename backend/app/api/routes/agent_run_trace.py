from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.run_traces import AgentRunTraceService
from app.schemas.run_trace import AgentRunTraceRead

router = APIRouter(prefix="/agent-runs", tags=["agent run trace"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}/trace", response_model=AgentRunTraceRead)
def get_agent_run_trace(run_id: UUID, db: DbSession) -> AgentRunTraceRead:
    trace = AgentRunTraceService(db).get(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return trace
