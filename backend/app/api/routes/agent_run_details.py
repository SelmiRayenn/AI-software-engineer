from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AgentRun
from app.schemas.agent_run import (
    AgentRunDetailBenchmarkTask,
    AgentRunDetailRead,
    AgentRunDetailRepository,
    AgentRunMetricSummary,
)

router = APIRouter(prefix="/agent-runs", tags=["agent runs"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}", response_model=AgentRunDetailRead)
def get_agent_run_detail(run_id: UUID, db: DbSession = None) -> AgentRunDetailRead:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")

    task = run.benchmark_task
    repository = task.repository
    generated_patch = run.generated_patch
    metric = run.evaluation_metric

    return AgentRunDetailRead(
        id=run.id,
        status=run.status,
        benchmark_task_id=run.benchmark_task_id,
        benchmark_task=AgentRunDetailBenchmarkTask(
            id=task.id,
            issue_number=task.issue_number,
            issue_title=task.issue_title,
        ),
        repository=AgentRunDetailRepository(
            name=repository.name,
            owner=repository.owner,
            url=repository.url,
        ),
        model_provider=run.model_provider,
        model_name=run.model_name,
        started_at=run.started_at,
        completed_at=run.completed_at,
        review_status=generated_patch.review_status if generated_patch else None,
        changed_files=generated_patch.changed_files if generated_patch else [],
        metric_summary=(
            AgentRunMetricSummary(
                id=metric.id,
                file_localization_score=metric.file_localization_score,
                patch_applied=metric.patch_applied,
                tests_passed=metric.tests_passed,
                modified_files_count=metric.modified_files_count,
                unrelated_files_count=metric.unrelated_files_count,
                tokens_used=metric.tokens_used,
                estimated_cost=metric.estimated_cost,
                execution_time_seconds=metric.execution_time_seconds,
                created_at=metric.created_at,
            )
            if metric
            else None
        ),
    )
