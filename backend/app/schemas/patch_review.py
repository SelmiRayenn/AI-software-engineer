from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.human_review import HumanReviewRead


class PatchApprovalRequest(BaseModel):
    reviewer_name: str = Field(min_length=1, max_length=255)
    review_notes: str | None = Field(default=None, max_length=20_000)
    update_existing: bool = False


class PatchRejectionRequest(BaseModel):
    reviewer_name: str = Field(min_length=1, max_length=255)
    review_notes: str = Field(min_length=1, max_length=20_000)
    update_existing: bool = False


class PatchReviewResponse(BaseModel):
    generated_patch_id: UUID
    review_status: str
    export_eligible: bool
    review: HumanReviewRead | None = None
