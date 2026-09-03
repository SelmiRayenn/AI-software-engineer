from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud
from app.db.session import get_db
from app.schemas.agent_run import AgentRunCreate, AgentRunRead

router = APIRouter(prefix="/agent-runs", tags=["agent runs"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[AgentRunRead])
def list_agent_runs(
    benchmark_task_id: UUID | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: DbSession = None,
) -> list[AgentRunRead]:
    return crud.list_agent_runs(
        db=db,
        benchmark_task_id=benchmark_task_id,
        skip=skip,
        limit=limit,
    )


@router.post("", response_model=AgentRunRead, status_code=status.HTTP_201_CREATED)
def create_agent_run(
    run_in: AgentRunCreate,
    db: DbSession = None,
) -> AgentRunRead:
    if crud.get_benchmark_task(db, run_in.benchmark_task_id) is None:
        raise HTTPException(status_code=404, detail="Benchmark task not found")
    return crud.create_agent_run(db=db, run_in=run_in)
