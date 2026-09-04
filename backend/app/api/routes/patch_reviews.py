from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.approvals import (
    PatchApprovalDuplicateReviewError,
    PatchApprovalError,
    PatchApprovalExportBlockedError,
    PatchApprovalService,
)
from app.db.session import get_db
from app.schemas.patch_review import (
    PatchApprovalRequest,
    PatchRejectionRequest,
    PatchReviewResponse,
)

router = APIRouter(prefix="/patches", tags=["patch reviews"])
DbSession = Annotated[Session, Depends(get_db)]
ActorTypeHeader = Annotated[str, Header(alias="X-Actor-Type")]


@router.post("/{patch_id}/approve", response_model=PatchReviewResponse)
def approve_patch(
    patch_id: UUID,
    request: PatchApprovalRequest,
    db: DbSession,
    actor_type: ActorTypeHeader = "human",
) -> PatchReviewResponse:
    _ensure_human_actor(actor_type)
    try:
        status_result = PatchApprovalService(db=db).approve_patch(
            patch_id,
            reviewer_name=request.reviewer_name,
            review_notes=request.review_notes,
            update_existing=request.update_existing,
        )
    except PatchApprovalError as exc:
        raise _http_error(exc) from exc
    return _response(status_result)


@router.post("/{patch_id}/reject", response_model=PatchReviewResponse)
def reject_patch(
    patch_id: UUID,
    request: PatchRejectionRequest,
    db: DbSession,
    actor_type: ActorTypeHeader = "human",
) -> PatchReviewResponse:
    _ensure_human_actor(actor_type)
    try:
        status_result = PatchApprovalService(db=db).reject_patch(
            patch_id,
            reviewer_name=request.reviewer_name,
            review_notes=request.review_notes,
            update_existing=request.update_existing,
        )
    except PatchApprovalError as exc:
        raise _http_error(exc) from exc
    return _response(status_result)


@router.get("/{patch_id}/review", response_model=PatchReviewResponse)
def get_patch_review(patch_id: UUID, db: DbSession) -> PatchReviewResponse:
    try:
        return _response(PatchApprovalService(db=db).review_status(patch_id))
    except PatchApprovalError as exc:
        raise _http_error(exc) from exc


def _response(status_result) -> PatchReviewResponse:
    return PatchReviewResponse(
        generated_patch_id=status_result.generated_patch_id,
        review_status=status_result.review_status,
        export_eligible=status_result.export_eligible,
        review=status_result.review,
    )


def _ensure_human_actor(actor_type: str) -> None:
    if actor_type.strip().lower() == "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent actors cannot approve, reject, or export generated patches.",
        )


def _http_error(exc: PatchApprovalError) -> HTTPException:
    if isinstance(exc, PatchApprovalDuplicateReviewError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, PatchApprovalExportBlockedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
