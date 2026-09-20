import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
MAX_PATCH_CHARS = 1_000_000
TASK_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._/-]*"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,254}/[A-Za-z0-9_.-]{1,255}")
TestIdentifier = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"^[^\x00]+$")]


class BenchmarkImportTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=255, pattern=f"^{TASK_ID_PATTERN}$")
    repo: str | None = Field(default=None, max_length=511)
    repo_url: str | None = Field(default=None, max_length=2048)
    issue_number: int | None = Field(default=None, gt=0, strict=True)
    problem_statement: str = Field(min_length=1, max_length=500_000)
    base_commit: str = Field(min_length=1, max_length=64)
    patch: str | None = Field(default=None, max_length=MAX_PATCH_CHARS)
    test_patch: str | None = Field(default=None, max_length=MAX_PATCH_CHARS)
    fail_to_pass: list[TestIdentifier] | None = Field(default=None, max_length=10_000)
    pass_to_pass: list[TestIdentifier] | None = Field(default=None, max_length=10_000)
    environment_setup_commit: str | None = Field(default=None, min_length=1, max_length=64)
    hints_text: str | None = Field(default=None, max_length=500_000)
    created_at: datetime | None = None

    @field_validator(
        "task_id",
        "repo",
        "repo_url",
        "base_commit",
        "environment_setup_commit",
        "problem_statement",
        mode="before",
    )
    @classmethod
    def strip_identifiers(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "base_commit",
        "environment_setup_commit",
        "problem_statement",
        "patch",
        "test_patch",
        "hints_text",
    )
    @classmethod
    def reject_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("NUL characters are not supported.")
        return value

    @field_validator("fail_to_pass", "pass_to_pass")
    @classmethod
    def nonblank_test_identifiers(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("Test identifiers must not be blank.")
        return value

    @model_validator(mode="after")
    def normalize_repository(self) -> "BenchmarkImportTask":
        if self.repo is None and self.repo_url is None:
            raise ValueError("repo or repo_url is required.")
        if self.repo is not None and (
            not REPOSITORY_PATTERN.fullmatch(self.repo) or self.repo.split("/")[1] in {".", ".."}
        ):
            raise ValueError("repo must be owner/name.")
        if self.repo_url is not None:
            url = urlsplit(self.repo_url)
            if (
                url.scheme != "https"
                or url.netloc.lower() != "github.com"
                or url.query
                or url.fragment
            ):
                raise ValueError("repo_url must be a public HTTPS GitHub repository URL.")
            url_repo = url.path.removeprefix("/").removesuffix("/").removesuffix(".git")
            if not REPOSITORY_PATTERN.fullmatch(url_repo) or url_repo.split("/")[1] in {".", ".."}:
                raise ValueError("repo_url must identify a repository, not an issue or subpath.")
            if self.repo is not None and self.repo.lower() != url_repo.lower():
                raise ValueError("repo and repo_url must identify the same repository.")
            self.repo = url_repo
        assert self.repo is not None
        self.repo = self.repo.lower()
        self.repo_url = f"https://github.com/{self.repo}"
        return self


class BenchmarkImportError(BaseModel):
    row: int | None
    task_id: str | None = None
    code: str
    message: str
    outcome: Literal["failed", "skipped"] = "failed"


class BenchmarkImportResult(BaseModel):
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    errors: list[BenchmarkImportError] = Field(default_factory=list)
    created_task_ids: list[UUID] = Field(default_factory=list)
