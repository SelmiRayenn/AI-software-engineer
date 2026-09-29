from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.redaction import redact_structured_value
from app.schemas.agent_plan import AgentPlanRead


class _SafeTraceModel(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def redact_trace_fields(cls, value):
        return redact_structured_value(value, max_string_chars=8_192)


class AgentRunTraceEvent(_SafeTraceModel):
    id: UUID
    created_at: datetime
    event_type: str
    summary: str
    sanitized_payload: dict[str, Any]
    tool_name: str | None = None
    file_paths: list[str]
    severity: Literal["info", "warning", "error"]


class TracePatchSummary(_SafeTraceModel):
    id: UUID
    version: int
    is_selected: bool
    changed_files: list[str]
    review_status: str
    created_at: datetime


class TraceTestPhaseSummary(_SafeTraceModel):
    phase: str
    command_count: int
    passed_count: int
    failed_count: int
    duration_seconds: float


class TraceFailureSummary(_SafeTraceModel):
    category: str
    summary: str
    source_event_id: UUID | None = None
    created_at: datetime


class TraceMetricSummary(_SafeTraceModel):
    file_localization_score: float | None
    patch_applied: bool
    baseline_tests_passed: bool
    post_patch_tests_passed: bool
    lint_passed: bool | None = None
    format_check_passed: bool | None = None
    code_quality_passed: bool | None = None
    code_quality_score: float = 0.0
    review_ready: bool = False
    review_blockers: list[str] = Field(default_factory=list)
    review_warnings: list[str] = Field(default_factory=list)
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


class AgentRunTraceRead(_SafeTraceModel):
    latest_plan: AgentPlanRead | None = None
    plan_status: Literal["not_submitted", "accepted", "rejected"] = "not_submitted"
    run_id: UUID
    status: str
    events: list[AgentRunTraceEvent]
    generated_patches: list[TracePatchSummary]
    test_phases: list[TraceTestPhaseSummary]
    failure: TraceFailureSummary | None = None
    metrics: TraceMetricSummary | None = None
