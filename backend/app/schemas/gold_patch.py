from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GoldPatchBase(BaseModel):
    benchmark_task_id: UUID
    changed_files: list[str] = Field(default_factory=list)
    patch_text: str
    test_files: list[str] = Field(default_factory=list)


class GoldPatchCreate(GoldPatchBase):
    pass


class GoldPatchRead(GoldPatchBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
