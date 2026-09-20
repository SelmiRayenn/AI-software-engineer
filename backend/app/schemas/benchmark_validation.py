from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ValidationSeverity = Literal["info", "warning", "error"]
ValidationOverallStatus = Literal["ready", "warning", "blocked"]


class ValidationReportItem(BaseModel):
    severity: ValidationSeverity
    code: str
    message: str
    recommendation: str


class ValidationReportSummary(BaseModel):
    total_count: int
    info_count: int
    warning_count: int
    error_count: int


class BenchmarkTaskValidationStatistics(BaseModel):
    pack_membership_count: int = 0
    hidden_test_count: int = 0
    enabled_hidden_test_count: int = 0
    imported: bool = False


class BenchmarkPackValidationStatistics(BaseModel):
    task_count: int = 0
    ready_task_count: int = 0
    draft_task_count: int = 0
    running_task_count: int = 0
    completed_task_count: int = 0
    failed_task_count: int = 0
    archived_task_count: int = 0
    unknown_status_task_count: int = 0
    repository_count: int = 0
    hidden_test_task_count: int = 0
    hidden_test_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    difficulty_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    tag_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_setup_command_count: int = 0
    missing_test_command_count: int = 0


class BenchmarkTaskValidationReport(BaseModel):
    subject_type: Literal["benchmark_task"] = "benchmark_task"
    subject_id: UUID
    overall_status: ValidationOverallStatus
    items: list[ValidationReportItem]
    summary: ValidationReportSummary
    statistics: BenchmarkTaskValidationStatistics


class BenchmarkPackValidationReport(BaseModel):
    subject_type: Literal["benchmark_pack"] = "benchmark_pack"
    subject_id: UUID
    overall_status: ValidationOverallStatus
    items: list[ValidationReportItem]
    summary: ValidationReportSummary
    statistics: BenchmarkPackValidationStatistics
