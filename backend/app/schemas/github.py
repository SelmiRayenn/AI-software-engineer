from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GitHubIssuePreviewRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)
    issue_number: int = Field(gt=0)


class GitHubPullRequestPreviewRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)
    pull_request_number: int = Field(gt=0)


class GitHubRepositoryPreview(BaseModel):
    owner: str
    name: str
    full_name: str
    url: str
    html_url: str
    default_branch: str
    language: str | None = None
    description: str | None = None
    private: bool = False


class GitHubUserPreview(BaseModel):
    login: str | None = None
    html_url: str | None = None


class GitHubIssuePreview(BaseModel):
    number: int
    title: str
    body: str | None = None
    state: str
    html_url: str
    user: GitHubUserPreview | None = None
    labels: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    is_pull_request: bool = False


class GitHubIssueCommentPreview(BaseModel):
    id: int
    body: str | None = None
    html_url: str
    user: GitHubUserPreview | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GitHubPullRequestFilePreview(BaseModel):
    filename: str
    status: str
    file_kind: Literal["source", "test", "docs", "config", "other"]
    previous_filename: str | None = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None


class GitHubPullRequestCommitPreview(BaseModel):
    sha: str
    message: str | None = None
    author_name: str | None = None
    author_date: datetime | None = None
    html_url: str | None = None


class GitHubPullRequestPreview(BaseModel):
    number: int
    title: str
    body: str | None = None
    state: str
    html_url: str
    merged: bool | None = None
    merge_commit_sha: str | None = None
    base_branch: str
    base_sha: str
    head_branch: str
    head_sha: str
    user: GitHubUserPreview | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    files: list[GitHubPullRequestFilePreview] = Field(default_factory=list)
    commits: list[GitHubPullRequestCommitPreview] = Field(default_factory=list)


class GitHubBenchmarkTaskHint(BaseModel):
    repository_url: str
    issue_number: int | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    base_commit: str | None = None
    fix_commit: str | None = None
    linked_pr_url: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)


class GitHubIssuePreviewResponse(BaseModel):
    repository: GitHubRepositoryPreview
    issue: GitHubIssuePreview
    comments: list[GitHubIssueCommentPreview]
    linked_pull_requests: list[GitHubPullRequestPreview]
    benchmark_task_hint: GitHubBenchmarkTaskHint


class GitHubPullRequestPreviewResponse(BaseModel):
    repository: GitHubRepositoryPreview
    pull_request: GitHubPullRequestPreview
    benchmark_task_hint: GitHubBenchmarkTaskHint


class GitHubHiddenTestCandidate(BaseModel):
    path: str
    status: str
    patch: str | None = None
    content: str | None = None
    content_available: bool
    suggested_commands: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class GitHubTrustedPullRequestPreviewResponse(GitHubPullRequestPreviewResponse):
    detected_test_files: list[str] = Field(default_factory=list)
    hidden_test_candidates: list[GitHubHiddenTestCandidate] = Field(default_factory=list)


class GitHubErrorResponse(BaseModel):
    detail: str
    github_status_code: int | None = None
    github_response: dict[str, Any] | None = None
