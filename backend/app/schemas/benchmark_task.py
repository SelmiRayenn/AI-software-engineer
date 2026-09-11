from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.task_statuses import VALID_TASK_STATUSES
from app.schemas.repository import RepositoryRead


class BenchmarkTaskBase(BaseModel):
    repository_id: UUID
    issue_number: int
    issue_title: str
    issue_body: str | None = None
    issue_comments: list[dict[str, str | None]] = Field(default_factory=list)
    pull_request_number: int | None = Field(default=None, ge=1)
    base_commit: str
    fix_commit: str | None = None
    linked_pr_url: str | None = None
    setup_commands: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    notes: str | None = None
    allow_lockfile_changes: bool = False
    allow_dependency_file_changes: bool = False
    status: str = "draft"

    @field_validator("status")
    @classmethod
    def validate_status(cls, status: str) -> str:
        if status not in VALID_TASK_STATUSES:
            allowed = ", ".join(sorted(VALID_TASK_STATUSES))
            raise ValueError(f"Task status must be one of: {allowed}")
        return status


class BenchmarkTaskCreate(BenchmarkTaskBase):
    pass


class BenchmarkTaskRead(BenchmarkTaskBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BenchmarkTaskFromGitHubRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)
    issue_number: int = Field(gt=0)
    pull_request_number: int = Field(gt=0)
    base_commit: str = Field(min_length=7, max_length=64)
    fix_commit: str | None = Field(default=None, min_length=7, max_length=64)
    setup_commands: list[str] = Field(default_factory=list, max_length=50)
    test_commands: list[str] = Field(min_length=1, max_length=50)
    notes: str | None = None
    allow_lockfile_changes: bool = False
    allow_dependency_file_changes: bool = False

    @field_validator("setup_commands", "test_commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            if not command.strip():
                raise ValueError("Commands must not be empty")
            if len(command) > 4000:
                raise ValueError("Commands must be 4000 characters or fewer")
        return commands


class AgentVisibleIssueComment(BaseModel):
    body: str | None = None
    html_url: str
    user_login: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentVisibleBenchmarkTaskRead(BaseModel):
    id: UUID
    repository_id: UUID
    repository: RepositoryRead
    issue_number: int
    issue_title: str
    issue_body: str | None = None
    issue_comments: list[AgentVisibleIssueComment] = Field(default_factory=list)
    base_commit: str
    status: str
    created_at: datetime


class BenchmarkTaskValidationResult(BaseModel):
    task_id: UUID
    status: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
