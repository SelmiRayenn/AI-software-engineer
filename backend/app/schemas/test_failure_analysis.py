from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TestFailureAnalysisRead(BaseModel):
    event_id: UUID | None = None
    test_result_id: UUID | None = None
    phase: str
    failed_command: str | None = None
    exit_code: int | None = None
    concise_failure_summary: str
    likely_failing_test_names: list[str] = Field(default_factory=list)
    assertion_error_excerpts: list[str] = Field(default_factory=list)
    stack_trace_snippets: list[str] = Field(default_factory=list)
    affected_file_paths: list[str] = Field(default_factory=list)
    timed_out: bool = False
    command_failed: bool = False
    output_truncated: bool = False
    hidden_details_redacted: bool = False
    failed_result_count: int = 1
    total_result_count: int = 1
    created_at: datetime | None = None
