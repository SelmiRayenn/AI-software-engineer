from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.run_statuses import VALID_RUN_STATUSES


class AgentRunBase(BaseModel):
    benchmark_task_id: UUID
    model_provider: str
    model_name: str
    status: str = "queued"

    @field_validator("status")
    @classmethod
    def validate_status(cls, status: str) -> str:
        if status not in VALID_RUN_STATUSES:
            allowed = ", ".join(sorted(VALID_RUN_STATUSES))
            raise ValueError(f"Run status must be one of: {allowed}")
        return status


class AgentRunCreate(AgentRunBase):
    pass


class AgentRunRead(AgentRunBase):
    id: UUID
    started_at: datetime
    completed_at: datetime | None = None
    patch_review_status: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentRunStartRequest(BaseModel):
    model_provider: str = Field(default="mock", min_length=1, max_length=100)
    model_name: str | None = Field(default=None, max_length=255)
    max_steps: int = Field(default=4, ge=1, le=50)
    command_timeout_seconds: int = Field(default=120, ge=1, le=600)


class AgentRunTraceStep(BaseModel):
    step_name: str
    success: bool
    duration_seconds: float
    summary: dict = Field(default_factory=dict)
    error_message: str | None = None
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)


class AgentRunStartResponse(BaseModel):
    id: UUID
    benchmark_task_id: UUID
    model_provider: str
    model_name: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    steps: list[AgentRunTraceStep] = Field(default_factory=list)
    generated_patch_id: UUID | None = None
    changed_files: list[str] = Field(default_factory=list)
    patch_review_status: str | None = None
    error_message: str | None = None
