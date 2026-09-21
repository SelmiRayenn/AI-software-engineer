from uuid import UUID

from pydantic import BaseModel


class AnalyticsSummary(BaseModel):
    total_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    approved_patches: int = 0
    rejected_patches: int = 0
    patch_apply_rate: float = 0.0
    visible_test_pass_rate: float = 0.0
    hidden_test_pass_rate: float | None = None
    issue_resolved_rate: float = 0.0
    regression_rate: float = 0.0
    average_file_localization_score: float = 0.0
    average_issue_specific_score: float = 0.0
    average_modified_files_count: float = 0.0
    average_unrelated_files_count: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    average_cost_per_run: float = 0.0
    average_execution_time_seconds: float = 0.0


class RepositoryAnalytics(AnalyticsSummary):
    repository_id: UUID
    repository_owner: str
    repository_name: str
    repository_url: str


class PackAnalytics(AnalyticsSummary):
    benchmark_pack_id: UUID
    pack_name: str
    pack_slug: str
    pack_version: str


class ModelLeaderboardRow(BaseModel):
    model_provider: str
    model_name: str
    total_runs: int
    completed_runs: int
    failed_runs: int
    issue_resolved_rate: float
    visible_test_pass_rate: float
    hidden_test_pass_rate: float | None
    average_file_localization_score: float
    average_issue_specific_score: float
    average_cost_per_run: float
    average_tokens_per_run: float
    average_execution_time_seconds: float
    average_modified_files_count: float
    average_unrelated_files_count: float
    rank_by_issue_resolved: int = 0
    rank_by_cost: int = 0
    rank_by_speed: int = 0
    rank_by_localization: int = 0
    composite_score: float
