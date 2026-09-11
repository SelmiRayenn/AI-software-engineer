from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.failures import AgentRunFailureNotFoundError, FailureClassificationService
from app.models import AgentEvent, AgentRun
from app.schemas.agent_run import (
    AgentPromptPreview,
    AgentRunConfig,
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
    failure = run.failure
    if failure is None and run.status in {"failed", "cancelled"}:
        try:
            failure = FailureClassificationService(db).get_or_classify(run.id)
        except AgentRunFailureNotFoundError:
            failure = None
    run_config, prompt_preview = _stored_run_context(db, run.id)

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
        run_config=run_config,
        prompt_preview=prompt_preview,
        repair_attempts_used=run.repair_attempts_used,
        final_patch_id=run.final_patch_id,
        final_patch_passed_tests=run.final_patch_passed_tests,
        failure_summary=run.failure_summary,
        failure_category=failure.category if failure else None,
    )


def _stored_run_context(
    db: Session,
    run_id: UUID,
) -> tuple[AgentRunConfig | None, AgentPromptPreview | None]:
    event = db.scalar(
        select(AgentEvent)
        .where(
            AgentEvent.agent_run_id == run_id,
            AgentEvent.event_type == "agent_run_configured",
        )
        .order_by(AgentEvent.created_at.desc())
        .limit(1)
    )
    if event is None:
        return None, None

    payload = event.payload_json or {}
    try:
        run_config = AgentRunConfig.model_validate(payload.get("config"))
        prompt_preview = AgentPromptPreview.model_validate(payload.get("prompt_preview"))
    except ValidationError:
        return None, None
    return run_config, prompt_preview
