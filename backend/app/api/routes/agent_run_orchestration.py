from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.orchestrator import (
    AgentRunOrchestrator,
    AgentRunStartError,
    BenchmarkTaskNotReadyError,
    GitSandboxWorkspacePreparer,
)
from app.db.session import get_db
from app.model_providers import ModelProviderConfigError, ModelProviderFactory
from app.schemas.agent_run import AgentRunStartRequest, AgentRunStartResponse

router = APIRouter(prefix="/agent-runs", tags=["agent runs"])
DbSession = Annotated[Session, Depends(get_db)]


def get_workspace_preparer() -> GitSandboxWorkspacePreparer:
    return GitSandboxWorkspacePreparer()


def get_model_provider_factory() -> ModelProviderFactory:
    return ModelProviderFactory()


WorkspacePreparerDep = Annotated[GitSandboxWorkspacePreparer, Depends(get_workspace_preparer)]
ModelProviderFactoryDep = Annotated[ModelProviderFactory, Depends(get_model_provider_factory)]


@router.post("/{benchmark_task_id}/start", response_model=AgentRunStartResponse)
def start_agent_run(
    benchmark_task_id: UUID,
    request: AgentRunStartRequest,
    db: DbSession,
    workspace_preparer: WorkspacePreparerDep,
    provider_factory: ModelProviderFactoryDep,
) -> AgentRunStartResponse:
    orchestrator = AgentRunOrchestrator(
        db=db,
        workspace_preparer=workspace_preparer,
        provider_factory=provider_factory,
    )
    try:
        return orchestrator.start_run(
            benchmark_task_id=benchmark_task_id,
            request=request,
        )
    except BenchmarkTaskNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ModelProviderConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentRunStartError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
