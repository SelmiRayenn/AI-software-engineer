from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReportText(BaseModel):
    text: str
    truncated: bool = False
    original_size_bytes: int


class ReportRunMetadata(BaseModel):
    id: UUID
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    repair_attempts_used: int
    final_patch_id: UUID | None = None
    final_patch_passed_tests: bool | None = None


class ReportBenchmarkTask(BaseModel):
    id: UUID
    status: str
    issue_number: int | None = None
    issue_title: str
    base_commit: str
    difficulty: str
    tags: list[str] = Field(default_factory=list)


class ReportRepository(BaseModel):
    id: UUID
    owner: str
    name: str
    url: str
    default_branch: str
    language: str | None = None


class ReportIssueSummary(BaseModel):
    title: str
    body: ReportText
    comment_count: int


class ReportModel(BaseModel):
    provider: str
    name: str


class ReportTraceEvent(BaseModel):
    created_at: datetime
    event_type: str
    summary: str
    tool_name: str | None = None
    file_paths: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "error"]


class ReportTraceSummary(BaseModel):
    total_events: int
    events_included: int
    truncated: bool
    event_counts: dict[str, int] = Field(default_factory=dict)
    events: list[ReportTraceEvent] = Field(default_factory=list)


class ReportPatchQuality(BaseModel):
    changed_file_count: int
    added_lines: int
    removed_lines: int
    total_changed_lines: int
    changed_source_files: list[str]
    changed_test_files: list[str]
    changed_docs_config_files: list[str]
    suspicious_generated_files: list[str]
    whitespace_only: bool
    dependency_files: list[str]
    lockfiles: list[str]
    warnings: list[str]
    hard_limit_violations: list[str]


class ReportTestCommand(BaseModel):
    command: str
    passed: bool
    exit_code: int
    duration_seconds: float | None = None
    stdout: ReportText
    stderr: ReportText


class ReportTestPhase(BaseModel):
    phase: str
    command_count: int
    passed_count: int
    failed_count: int
    duration_seconds: float
    details_included: bool
    results_truncated: bool = False
    results: list[ReportTestCommand] = Field(default_factory=list)


class ReportHiddenEvaluation(BaseModel):
    available: bool
    passed: bool | None = None
    run_count: int = 0
    failed_count: int = 0


class ReportEvaluationMetrics(BaseModel):
    file_localization_score: float | None = None
    patch_applied: bool
    tests_passed: bool
    baseline_tests_passed: bool
    post_patch_tests_passed: bool
    hidden_tests_passed: bool | None = None
    hidden_tests_run_count: int
    hidden_tests_failed_count: int
    issue_resolved: bool
    regression_detected: bool
    issue_specific_score: float
    modified_files_count: int
    unrelated_files_count: int

    model_config = ConfigDict(from_attributes=True)


class ReportFailure(BaseModel):
    category: str
    summary: ReportText
    created_at: datetime


class ReportHumanReview(BaseModel):
    status: str
    reviewer_name: str | None = None
    review_notes: ReportText | None = None
    reviewed_at: datetime | None = None


class ReportUsageSummary(BaseModel):
    tokens_used: int
    estimated_cost: float
    execution_time_seconds: float | None = None


class ReportFinalPatch(BaseModel):
    id: UUID
    version: int
    is_selected: bool
    changed_files: list[str]
    review_status: str
    diff: ReportText
    reference: str


class AgentRunReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: datetime
    run: ReportRunMetadata
    benchmark_task: ReportBenchmarkTask
    repository: ReportRepository
    issue: ReportIssueSummary
    model: ReportModel
    run_configuration: dict[str, Any] | None = None
    trace: ReportTraceSummary
    files_inspected: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    final_patch: ReportFinalPatch | None = None
    patch_quality: ReportPatchQuality | None = None
    test_results: list[ReportTestPhase] = Field(default_factory=list)
    hidden_evaluation: ReportHiddenEvaluation
    evaluation_metrics: ReportEvaluationMetrics | None = None
    failure: ReportFailure | None = None
    human_review: ReportHumanReview
    usage: ReportUsageSummary
