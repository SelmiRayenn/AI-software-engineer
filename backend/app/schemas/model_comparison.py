from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_run import AgentRunStartRequest


class ComparisonModel(BaseModel):
    model_provider: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=255)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def normalize_provider(self) -> "ComparisonModel":
        self.model_provider = self.model_provider.lower()
        return self


class ModelComparisonRequest(BaseModel):
    models: list[ComparisonModel] = Field(min_length=2, max_length=10)
    max_steps: int = Field(default=4, ge=1, le=50)
    max_tool_errors: int = Field(default=3, ge=1, le=20)
    command_timeout_seconds: int = Field(default=120, ge=1, le=600)
    include_issue_comments: bool = True
    enable_test_tool: bool = True
    run_mode: Literal["scripted", "tool_loop"] = "tool_loop"

    model_config = ConfigDict(extra="forbid")

    def run_requests(self) -> list[AgentRunStartRequest]:
        options = self.model_dump(exclude={"models"})
        return [AgentRunStartRequest(**options, **model.model_dump()) for model in self.models]

    @model_validator(mode="after")
    def validate_models(self) -> "ModelComparisonRequest":
        identities = {(model.model_provider, model.model_name) for model in self.models}
        if len(identities) != len(self.models):
            raise ValueError("Each provider/model pair must appear only once per comparison.")
        self.run_requests()
        return self


class ModelComparisonRun(BaseModel):
    run_id: UUID
    provider: str
    model: str
    status: str
    metric_id: UUID | None = None
    patch_applied: bool | None = None
    tests_passed: bool | None = None
    file_localization_score: float | None = None
    modified_files_count: int | None = None
    unrelated_files_count: int | None = None
    tokens_used: int | None = None
    estimated_cost: float | None = None
    execution_time_seconds: float | None = None
    failure_reason: str | None = None


class ComparisonWinner(BaseModel):
    run_id: UUID
    provider: str
    model: str


class ModelComparisonAggregates(BaseModel):
    best_passing_model: ComparisonWinner | None = None
    lowest_cost_passing_model: ComparisonWinner | None = None
    fastest_passing_model: ComparisonWinner | None = None
    highest_localization_score: float | None = None
    total_cost: float = 0.0
    total_execution_time: float = 0.0


class ModelComparisonResponse(BaseModel):
    comparison_id: UUID
    benchmark_task_id: UUID
    created_at: datetime
    status: Literal["queued", "running", "completed", "partial_failure", "failed"]
    runs: list[ModelComparisonRun]
    aggregates: ModelComparisonAggregates
