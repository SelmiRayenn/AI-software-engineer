from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class HumanReviewBase(BaseModel):
    generated_patch_id: UUID
    decision: str
    reviewer_name: str | None = None
    review_notes: str | None = None


class HumanReviewCreate(HumanReviewBase):
    pass


class HumanReviewRead(HumanReviewBase):
    id: UUID
    reviewed_at: datetime

    model_config = ConfigDict(from_attributes=True)
