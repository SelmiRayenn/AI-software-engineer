from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.redaction import redact_structured_value
from app.schemas.run_trace import TraceFailureSummary, TraceMetricSummary, TraceTestPhaseSummary


class _SafeReplayModel(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def redact_replay_fields(cls, value):
        return redact_structured_value(value, max_string_chars=8_192)


class ReplayText(_SafeReplayModel):
    text: str
    original_size_bytes: int
    truncated: bool = False
    redacted: bool = False


class ReplayRepositoryContext(_SafeReplayModel):
    id: UUID
    owner: str
    name: str
    url: str
    default_branch: str
    language: str | None = None


class ReplayBenchmarkContext(_SafeReplayModel):
    id: UUID
    issue_number: int | None = None
    issue_title: str
    issue_body: ReplayText
    issue_comments: list[dict[str, ReplayText | str | None]] = Field(default_factory=list)
    base_commit: str
    configured_test_commands: list[str] = Field(default_factory=list)
    repository: ReplayRepositoryContext


class ReplayModelContext(_SafeReplayModel):
    provider: str
    model: str


class ReplayPromptSections(_SafeReplayModel):
    system_prompt: ReplayText | None = None
    developer_safety_prompt: ReplayText | None = None
    issue_context_prompt: ReplayText | None = None
    tool_use_instructions: ReplayText | None = None
    patch_submission_instructions: ReplayText | None = None


class ReplayToolCall(_SafeReplayModel):
    sequence: int
    event_id: UUID
    created_at: datetime
    tool_name: str
    status: str
    sanitized_input: Any = None
    sanitized_output: Any = None
    file_paths: list[str] = Field(default_factory=list)
    error_summary: str | None = None


class ReplayModelResponse(_SafeReplayModel):
    sequence: int
    event_id: UUID
    created_at: datetime
    summary: str
    success: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    latency_seconds: float | None = None
    content_preview: Any = None
    requested_tools: list[str] = Field(default_factory=list)


class ReplayPatchVersion(_SafeReplayModel):
    id: UUID
    version: int
    is_selected: bool
    changed_files: list[str] = Field(default_factory=list)
    review_status: str
    created_at: datetime


class ReplayTimestamps(_SafeReplayModel):
    run_started_at: datetime
    run_completed_at: datetime | None = None
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None


class ReplayIntegrityMetadata(_SafeReplayModel):
    snapshot_created_at: datetime
    source_commit_sha: str | None = None
    app_version: str
    run_event_count: int
    patch_count: int
    test_result_count: int
    checksum_sha256: str
    redaction_applied: bool
    truncation_applied: bool


class AgentRunReplaySnapshot(_SafeReplayModel):
    schema_version: str = "1.0"
    run_id: UUID
    run_status: str
    benchmark_context: ReplayBenchmarkContext
    model: ReplayModelContext
    run_configuration: dict[str, Any]
    rendered_prompt_sections: ReplayPromptSections
    allowed_tools: list[str]
    tool_call_sequence: list[ReplayToolCall]
    model_response_summaries: list[ReplayModelResponse]
    patch_versions: list[ReplayPatchVersion]
    test_phases: list[TraceTestPhaseSummary]
    failure_classification: TraceFailureSummary | None = None
    evaluation_metrics: TraceMetricSummary | None = None
    timestamps: ReplayTimestamps
    integrity: ReplayIntegrityMetadata
