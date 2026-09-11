from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AgentRun
from app.patches import (
    PatchApplyError,
    PatchError,
    PatchSafetyError,
    PatchService,
    PatchWorkspaceError,
    calculate_patch_size_statistics,
)
from app.schemas.generated_patch import GeneratedPatchRead
from app.schemas.patch import (
    GeneratedPatchResponse,
    PatchApplyRequest,
    PatchApplyResponse,
    PatchSizeStatsRead,
    WorkspaceDiffRead,
)

router = APIRouter(prefix="/agent-runs", tags=["agent run patches"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}/patches", response_model=list[GeneratedPatchRead])
def list_agent_run_patches(run_id: UUID, db: DbSession) -> list:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return run.generated_patches


@router.get("/{run_id}/diff", response_model=WorkspaceDiffRead)
def get_agent_run_diff(run_id: UUID, db: DbSession = None) -> WorkspaceDiffRead:
    service = _patch_service(db, run_id)
    try:
        diff = service.get_current_workspace_diff()
    except PatchError as exc:
        raise _http_error(exc) from exc
    return WorkspaceDiffRead(
        patch_text=diff.patch_text,
        changed_files=diff.changed_files,
        stats=PatchSizeStatsRead(**diff.stats.__dict__),
    )


@router.get("/{run_id}/patch", response_model=GeneratedPatchResponse)
def get_agent_run_patch(run_id: UUID, db: DbSession = None) -> GeneratedPatchResponse:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found.")
    generated_patch = run.generated_patch
    if generated_patch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Generated patch not found."
        )

    stats = calculate_patch_size_statistics(
        generated_patch.patch_text,
        generated_patch.changed_files,
    )
    return GeneratedPatchResponse(
        id=generated_patch.id,
        agent_run_id=generated_patch.agent_run_id,
        patch_text=generated_patch.patch_text,
        changed_files=generated_patch.changed_files,
        stats=PatchSizeStatsRead(**stats.__dict__),
        review_status=generated_patch.review_status,
        created_at=generated_patch.created_at,
        version=generated_patch.version,
        is_selected=generated_patch.is_selected,
    )


@router.post("/{run_id}/patch/apply", response_model=PatchApplyResponse)
def apply_agent_run_patch(
    run_id: UUID,
    request: PatchApplyRequest,
    db: DbSession = None,
) -> PatchApplyResponse:
    service = _patch_service(db, run_id)
    try:
        result = service.apply_unified_diff(request.patch_text)
    except PatchError as exc:
        raise _http_error(exc) from exc

    return PatchApplyResponse(
        generated_patch_id=result.generated_patch_id,
        agent_run_id=run_id,
        patch_text=result.patch_text,
        changed_files=result.changed_files,
        stats=PatchSizeStatsRead(**result.stats.__dict__),
        review_status="pending",
    )


def _patch_service(db: Session, run_id: UUID) -> PatchService:
    try:
        return PatchService(db=db, agent_run_id=run_id)
    except PatchError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: PatchError) -> HTTPException:
    if isinstance(exc, PatchWorkspaceError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (PatchSafetyError, PatchApplyError)):
        return HTTPException(
            status_code=422,
            detail=str(exc),
        )
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
