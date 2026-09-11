from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PatchSizeStatsRead(BaseModel):
    size_bytes: int
    changed_files_count: int
    additions: int
    deletions: int


class WorkspaceDiffRead(BaseModel):
    patch_text: str
    changed_files: list[str] = Field(default_factory=list)
    stats: PatchSizeStatsRead


class PatchApplyRequest(BaseModel):
    patch_text: str = Field(min_length=1)


class PatchApplyResponse(BaseModel):
    generated_patch_id: UUID
    agent_run_id: UUID
    patch_text: str
    changed_files: list[str] = Field(default_factory=list)
    stats: PatchSizeStatsRead
    review_status: str = "pending"


class GeneratedPatchResponse(BaseModel):
    id: UUID
    agent_run_id: UUID
    patch_text: str
    changed_files: list[str] = Field(default_factory=list)
    stats: PatchSizeStatsRead
    review_status: str = "pending"
    created_at: datetime
    version: int = 1
    is_selected: bool = False
