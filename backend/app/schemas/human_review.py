from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.redaction import redact_and_truncate


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

    @field_validator("reviewer_name", "review_notes", mode="before")
    @classmethod
    def redact_review_text(cls, value: str | None) -> str | None:
        return redact_and_truncate(value, max_chars=8_000) if value is not None else None
