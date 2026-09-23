from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.analytics import AnalyticsSummary, ModelLeaderboardRow


class PublicDemoFilters(BaseModel):
    benchmark_pack_id: UUID | None = None
    model_provider: str | None = None
    model_name: str | None = None
    limit: int


class PublicDemoPack(BaseModel):
    id: UUID
    name: str
    slug: str
    version: str


class PublicDemoUsageSummary(BaseModel):
    total_tokens: int = 0
    total_estimated_cost: float = 0.0
    average_cost_per_run: float = 0.0
    total_execution_time_seconds: float = 0.0
    average_execution_time_seconds: float = 0.0


class PublicDemoRun(BaseModel):
    run_id: UUID
    benchmark_task_id: UUID
    repository_owner: str
    repository_name: str
    repository_url: str | None = None
    issue_number: int | None = None
    issue_title: str
    model_provider: str
    model_name: str
    run_status: str
    issue_resolved: bool
    visible_tests_passed: bool
    hidden_tests_passed: bool | None = None
    hidden_tests_run_count: int = 0
    file_localization_score: float | None = None
    issue_specific_score: float = 0.0
    tokens_used: int = 0
    estimated_cost: float = 0.0
    execution_time_seconds: float = 0.0
    failure_category: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class PublicDemoSnapshot(BaseModel):
    schema_version: str = "1.0"
    generated_at: datetime
    filters: PublicDemoFilters
    benchmark_pack: PublicDemoPack | None = None
    aggregate_metrics: AnalyticsSummary
    model_leaderboard: list[ModelLeaderboardRow] = Field(default_factory=list)
    usage: PublicDemoUsageSummary
    successful_runs: list[PublicDemoRun] = Field(default_factory=list)
    failed_runs: list[PublicDemoRun] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
