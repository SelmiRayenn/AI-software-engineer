from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.agent_run import AgentRunConfig, AgentRunStartRequest
from app.schemas.benchmark_task import AgentVisibleBenchmarkTaskRead
from app.schemas.failure import FailureCategory


class BenchmarkPackRunRequest(AgentRunConfig):
    model_config = ConfigDict(extra="forbid")

    include_hidden_tests: bool = False
    stop_on_task_failure: bool = False
    # Only include_hidden_tests may request the trusted evaluation path at this boundary.
    run_hidden_tests: Literal[False] = False

    def agent_request(self, model_name: str) -> AgentRunStartRequest:
        return AgentRunStartRequest.model_validate(
            {
                **self.model_dump(exclude={"include_hidden_tests", "stop_on_task_failure"}),
                "model_name": model_name,
                "run_hidden_tests": self.include_hidden_tests,
            }
        )


class PackTaskMetrics(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    patch_applied: bool = False
    baseline_tests_passed: bool = False
    post_patch_tests_passed: bool = False
    hidden_tests_passed: bool | None = None
    hidden_tests_run_count: int = 0
    hidden_tests_failed_count: int = 0
    issue_resolved: bool = False
    regression_detected: bool = False
    file_localization_score: float | None = None
    issue_specific_score: float = 0.0
    tokens_used: int | None = 0
    estimated_cost: float | None = 0.0
    execution_time_seconds: float | None = 0.0


class BenchmarkPackRunAggregates(BaseModel):
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    skipped_tasks: int = 0
    issue_resolved_count: int = 0
    issue_resolved_rate: float = 0.0
    visible_test_pass_rate: float = 0.0
    hidden_test_pass_rate: float | None = None
    hidden_tested_tasks: int = 0
    average_file_localization_score: float = 0.0
    average_issue_specific_score: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_execution_time: float = 0.0
    average_execution_time: float = 0.0


class BenchmarkPackRunTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    benchmark_task_id: UUID
    agent_run_id: UUID
    order_index: int
    task_snapshot: AgentVisibleBenchmarkTaskRead
    status: str
    metric_summary: PackTaskMetrics | None = None
    failure_category: FailureCategory | None = None
    failure_summary: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BenchmarkPackRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    benchmark_pack_id: UUID
    pack_name: str
    pack_slug: str
    pack_version: str
    model_provider: str
    model_name: str
    run_config: AgentRunConfig
    include_hidden_tests: bool
    stop_on_task_failure: bool
    status: str
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None = None
    aggregates: BenchmarkPackRunAggregates
    tasks: list[BenchmarkPackRunTaskRead]
