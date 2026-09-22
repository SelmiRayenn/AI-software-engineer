from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AgentRunTraceEvent(BaseModel):
    id: UUID
    created_at: datetime
    event_type: str
    summary: str
    sanitized_payload: dict[str, Any]
    tool_name: str | None = None
    file_paths: list[str]
    severity: Literal["info", "warning", "error"]


class TracePatchSummary(BaseModel):
    id: UUID
    version: int
    is_selected: bool
    changed_files: list[str]
    review_status: str
    created_at: datetime


class TraceTestPhaseSummary(BaseModel):
    phase: str
    command_count: int
    passed_count: int
    failed_count: int
    duration_seconds: float


class TraceFailureSummary(BaseModel):
    category: str
    summary: str
    source_event_id: UUID | None = None
    created_at: datetime


class TraceMetricSummary(BaseModel):
    file_localization_score: float | None
    patch_applied: bool
    baseline_tests_passed: bool
    post_patch_tests_passed: bool
    hidden_tests_passed: bool | None
    hidden_tests_run_count: int
    issue_resolved: bool
    regression_detected: bool
    issue_specific_score: float
    modified_files_count: int
    unrelated_files_count: int
    tokens_used: int | None
    estimated_cost: float | None
    execution_time_seconds: float | None

    model_config = ConfigDict(from_attributes=True)


class AgentRunTraceRead(BaseModel):
    run_id: UUID
    status: str
    events: list[AgentRunTraceEvent]
    generated_patches: list[TracePatchSummary]
    test_phases: list[TraceTestPhaseSummary]
    failure: TraceFailureSummary | None = None
    metrics: TraceMetricSummary | None = None
