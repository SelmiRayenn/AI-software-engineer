from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.run_statuses import VALID_RUN_STATUSES
from app.schemas.failure import FailureCategory


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
    repair_attempts_used: int = 0
    final_patch_id: UUID | None = None
    final_patch_passed_tests: bool | None = None
    failure_summary: str | None = None
    failure_category: FailureCategory | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentRunDetailRepository(BaseModel):
    name: str
    owner: str
    url: str


class AgentRunDetailBenchmarkTask(BaseModel):
    id: UUID
    issue_number: int
    issue_title: str


class AgentRunMetricSummary(BaseModel):
    id: UUID
    file_localization_score: float | None = None
    patch_applied: bool
    tests_passed: bool
    modified_files_count: int
    unrelated_files_count: int
    tokens_used: int | None = None
    estimated_cost: float | None = None
    execution_time_seconds: float | None = None
    created_at: datetime


class AgentRunConfig(BaseModel):
    model_provider: str = Field(default="mock", min_length=1, max_length=100)
    model_name: str | None = Field(default=None, max_length=255)
    max_steps: int = Field(default=4, ge=1, le=50)
    max_tool_errors: int = Field(default=3, ge=1, le=20)
    command_timeout_seconds: int = Field(default=120, ge=1, le=600)
    include_issue_comments: bool = True
    enable_test_tool: bool = True
    run_mode: Literal["scripted", "tool_loop"] = "tool_loop"
    max_repair_attempts: int = Field(default=0, ge=0, le=5, strict=True)
    run_tests_after_patch: bool = True
    stop_on_first_passing_patch: bool = True
    include_test_failure_feedback: bool = True

    @field_validator("model_provider")
    @classmethod
    def normalize_provider(cls, provider: str) -> str:
        normalized = provider.strip().lower()
        if not normalized:
            raise ValueError("model_provider must not be blank")
        return normalized

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, model_name: str | None) -> str | None:
        if model_name is None:
            return None
        normalized = model_name.strip()
        if not normalized:
            raise ValueError("model_name must not be blank when provided")
        return normalized

    @model_validator(mode="after")
    def validate_scripted_provider(self) -> "AgentRunConfig":
        if self.run_mode == "scripted" and self.model_provider != "mock":
            raise ValueError("scripted run mode requires the mock model provider")
        return self


class AgentPromptPreview(BaseModel):
    system_prompt: str
    developer_safety_prompt: str
    issue_context_prompt: str
    tool_use_instructions: str
    patch_submission_instructions: str


class AgentRunDetailRead(BaseModel):
    id: UUID
    status: str
    benchmark_task_id: UUID
    benchmark_task: AgentRunDetailBenchmarkTask
    repository: AgentRunDetailRepository
    model_provider: str
    model_name: str
    started_at: datetime
    completed_at: datetime | None = None
    review_status: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    metric_summary: AgentRunMetricSummary | None = None
    run_config: AgentRunConfig | None = None
    prompt_preview: AgentPromptPreview | None = None
    repair_attempts_used: int = 0
    final_patch_id: UUID | None = None
    final_patch_passed_tests: bool | None = None
    failure_summary: str | None = None
    failure_category: FailureCategory | None = None


class AgentRunStartRequest(AgentRunConfig):
    pass


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
    run_config: AgentRunConfig
    prompt_preview: AgentPromptPreview
    repair_attempts_used: int = 0
    final_patch_id: UUID | None = None
    final_patch_passed_tests: bool | None = None
    failure_summary: str | None = None
    failure_category: FailureCategory | None = None
