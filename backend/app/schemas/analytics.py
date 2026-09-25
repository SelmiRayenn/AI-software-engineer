from uuid import UUID

from pydantic import BaseModel, Field


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


class ToolUsageCount(BaseModel):
    tool_name: str
    call_count: int


class ToolFailureCount(BaseModel):
    tool_name: str
    failed_count: int


class ToolErrorsByModel(BaseModel):
    model_provider: str
    model_name: str
    total_tool_calls: int
    failed_tool_calls: int
    unknown_tool_calls: int
    malformed_tool_calls: int
    tool_error_rate: float
    runs_with_tool_errors: int


class ToolUsageAnalytics(BaseModel):
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    failed_tool_calls: int = 0
    unknown_tool_calls: int = 0
    malformed_tool_calls: int = 0
    tool_error_rate: float = 0.0
    average_tool_calls_per_run: float = 0.0
    most_used_tools: list[ToolUsageCount] = Field(default_factory=list)
    most_failed_tools: list[ToolFailureCount] = Field(default_factory=list)
    tool_error_counts_by_type: dict[str, int] = Field(default_factory=dict)
    runs_with_tool_errors: int = 0
    tool_errors_by_model: list[ToolErrorsByModel] = Field(default_factory=list)


class LocalizationMetricSet(BaseModel):
    total_runs_with_gold_files: int = 0
    runs_with_candidate_files: int = 0
    average_file_localization_score: float = 0.0
    top1_accuracy: float = 0.0
    top3_accuracy: float = 0.0
    top5_accuracy: float = 0.0
    edited_file_precision: float = 0.0
    edited_file_recall: float = 0.0
    average_files_read: float = 0.0
    average_files_edited: float = 0.0
    candidate_top1_accuracy: float = 0.0
    candidate_top3_accuracy: float = 0.0
    candidate_top5_accuracy: float = 0.0
    average_candidate_count: float = 0.0


class CandidateHitRateByModel(BaseModel):
    model_provider: str
    model_name: str
    runs_with_candidate_files: int
    candidate_hit_rate: float


class MissedGoldFile(BaseModel):
    repository_owner: str
    repository_name: str
    file_path: str
    missed_run_count: int
    gold_run_count: int
    miss_rate: float


class LocalizationByModel(LocalizationMetricSet):
    model_provider: str
    model_name: str


class LocalizationByRepository(LocalizationMetricSet):
    repository_id: UUID
    repository_owner: str
    repository_name: str
    repository_url: str


class FileLocalizationAnalytics(LocalizationMetricSet):
    most_common_missed_gold_files: list[MissedGoldFile] = Field(default_factory=list)
    localization_by_model: list[LocalizationByModel] = Field(default_factory=list)
    localization_by_repository: list[LocalizationByRepository] = Field(default_factory=list)
    candidate_hit_rate_by_model: list[CandidateHitRateByModel] = Field(default_factory=list)
