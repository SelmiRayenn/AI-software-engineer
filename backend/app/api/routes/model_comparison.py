from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.agents.comparison import ModelComparisonNotFoundError, ModelComparisonService
from app.agents.orchestrator import AgentRunOrchestrator, BenchmarkTaskNotReadyError
from app.api.routes.agent_run_orchestration import (
    DbSession,
    ModelProviderFactoryDep,
    WorkspacePreparerDep,
)
from app.schemas.model_comparison import ModelComparisonRequest, ModelComparisonResponse

router = APIRouter(prefix="/benchmark-tasks", tags=["model comparison"])


@router.post("/{task_id}/compare-models", response_model=ModelComparisonResponse)
def compare_models(
    task_id: UUID,
    request: ModelComparisonRequest,
    db: DbSession,
    workspace_preparer: WorkspacePreparerDep,
    provider_factory: ModelProviderFactoryDep,
) -> ModelComparisonResponse:
    service = ModelComparisonService(
        db=db,
        orchestrator=AgentRunOrchestrator(
            db=db, workspace_preparer=workspace_preparer, provider_factory=provider_factory
        ),
    )
    try:
        return service.compare(benchmark_task_id=task_id, request=request)
    except ModelComparisonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BenchmarkTaskNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{task_id}/model-comparison", response_model=ModelComparisonResponse)
def get_model_comparison(
    task_id: UUID, db: DbSession, comparison_id: UUID | None = None
) -> ModelComparisonResponse:
    try:
        return ModelComparisonService(db=db).get_comparison(
            benchmark_task_id=task_id, comparison_id=comparison_id
        )
    except ModelComparisonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
