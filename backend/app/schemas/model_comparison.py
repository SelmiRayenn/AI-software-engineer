from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_run import AgentRunStartRequest
from app.schemas.failure import FailureCategory


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
    max_steps: int = Field(default=6, ge=1, le=50)
    max_tool_errors: int = Field(default=3, ge=1, le=20)
    command_timeout_seconds: int = Field(default=120, ge=1, le=600)
    include_issue_comments: bool = True
    enable_test_tool: bool = True
    run_mode: Literal["scripted", "tool_loop"] = "tool_loop"
    max_repair_attempts: int = Field(default=0, ge=0, le=5, strict=True)
    run_tests_after_patch: bool = True
    stop_on_first_passing_patch: bool = True
    include_test_failure_feedback: bool = True
    require_hypothesis_update_after_failure: bool = True
    require_plan_update_after_failure: bool = False
    require_candidate_update_after_failure: bool = False
    max_failure_feedback_chars: int = Field(default=4096, ge=512, le=16384, strict=True)
    require_plan_before_edit: bool = True
    max_plan_revisions: int = Field(default=2, ge=0, le=10, strict=True)
    plan_min_evidence_files: int = Field(default=1, ge=1, le=50, strict=True)
    require_hypothesis_before_patch: bool = True
    require_candidate_files_before_edit: bool = True
    max_candidate_files: int = Field(default=10, ge=1, le=50, strict=True)
    enable_targeted_tests: bool = False
    targeted_tests_max_commands: int = Field(default=3, ge=1, le=20, strict=True)
    targeted_tests_trusted_gold_files: Literal[False] = False

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
    baseline_tests_passed: bool | None = None
    post_patch_tests_passed: bool | None = None
    hidden_tests_passed: bool | None = None
    hidden_tests_run_count: int | None = None
    hidden_tests_failed_count: int | None = None
    issue_resolved: bool | None = None
    regression_detected: bool | None = None
    issue_specific_score: float | None = None
    file_localization_score: float | None = None
    modified_files_count: int | None = None
    unrelated_files_count: int | None = None
    tokens_used: int | None = None
    estimated_cost: float | None = None
    execution_time_seconds: float | None = None
    failure_reason: str | None = None
    repair_attempts_used: int = 0
    final_patch_id: UUID | None = None
    final_patch_passed_tests: bool | None = None
    failure_summary: str | None = None
    failure_category: FailureCategory | None = None


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
