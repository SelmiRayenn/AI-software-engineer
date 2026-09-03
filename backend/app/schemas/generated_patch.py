from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GeneratedPatchBase(BaseModel):
    agent_run_id: UUID
    patch_text: str
    changed_files: list[str] = Field(default_factory=list)


class GeneratedPatchCreate(GeneratedPatchBase):
    pass


class GeneratedPatchRead(GeneratedPatchBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
