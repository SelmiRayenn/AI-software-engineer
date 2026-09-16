from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EvaluationMetricBase(BaseModel):
    agent_run_id: UUID
    file_localization_score: float | None = None
    patch_applied: bool = False
    tests_passed: bool = False
    baseline_tests_passed: bool = False
    post_patch_tests_passed: bool = False
    hidden_tests_passed: bool | None = None
    hidden_tests_run_count: int = 0
    hidden_tests_failed_count: int = 0
    issue_resolved: bool = False
    regression_detected: bool = False
    issue_specific_score: float = 0.0
    modified_files_count: int = 0
    unrelated_files_count: int = 0
    tokens_used: int | None = None
    estimated_cost: float | None = None
    execution_time_seconds: float | None = None


class EvaluationMetricCreate(EvaluationMetricBase):
    pass


class EvaluationMetricRead(EvaluationMetricBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
