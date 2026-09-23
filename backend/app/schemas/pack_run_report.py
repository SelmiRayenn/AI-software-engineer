from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.agent_run import AgentRunConfig
from app.schemas.benchmark_pack_run import BenchmarkPackRunAggregates, PackTaskMetrics
from app.schemas.run_report import ReportText


class PackReportMetadata(BaseModel):
    id: UUID
    name: str
    slug: str
    description: ReportText | None = None
    version: str
    source: str | None = None


class PackRunReportMetadata(BaseModel):
    id: UUID
    status: str
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None = None
    include_hidden_tests: bool
    stop_on_task_failure: bool


class PackReportModel(BaseModel):
    provider: str
    name: str


class PackReportTask(BaseModel):
    id: UUID
    order_index: int
    benchmark_task_id: UUID
    agent_run_id: UUID
    repository_owner: str
    repository_name: str
    repository_url: str
    issue_number: int | None = None
    issue_title: str
    difficulty: str
    tags: list[str] = Field(default_factory=list)
    status: str
    metrics: PackTaskMetrics | None = None
    failure_category: str | None = None
    failure_summary: ReportText | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PackReportFailureBreakdown(BaseModel):
    category: str
    count: int


class PackReportRecurringFailure(BaseModel):
    category: str
    summary: ReportText
    count: int
    benchmark_task_ids: list[UUID] = Field(default_factory=list)
    issue_titles: list[str] = Field(default_factory=list)


class BenchmarkPackRunReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: datetime
    pack: PackReportMetadata
    pack_run: PackRunReportMetadata
    model: PackReportModel
    run_configuration: AgentRunConfig
    aggregates: BenchmarkPackRunAggregates
    tasks: list[PackReportTask] = Field(default_factory=list)
    failure_breakdown: list[PackReportFailureBreakdown] = Field(default_factory=list)
    recurring_failures: list[PackReportRecurringFailure] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
