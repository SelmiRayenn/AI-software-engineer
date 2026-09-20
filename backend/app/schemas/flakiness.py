from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.sandbox import SandboxCommandResult

FlakinessStatus = Literal["stable", "flaky", "failed_setup", "inconclusive"]


class FlakinessCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repetitions: int = Field(default=3, ge=2, le=20)
    stop_on_first_failure: bool = False
    command_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class FlakinessCheckRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    repetition_number: int
    passed: bool
    duration_seconds: float
    command_results: list[SandboxCommandResult]
    created_at: datetime


class FlakinessCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    benchmark_task_id: UUID
    repetitions_requested: int
    repetitions_completed: int
    pass_count: int
    fail_count: int
    inconsistent_results: bool
    average_duration_seconds: float
    status: FlakinessStatus
    stop_on_first_failure: bool
    command_timeout_seconds: int
    workspace_id: str
    workspace_retained: bool
    setup_results: list[SandboxCommandResult]
    error_code: str | None = None
    error_summary: str | None = None
    runs: list[FlakinessCheckRunRead]
    created_at: datetime
