from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.task_metadata import TaskDifficulty, normalize_difficulty, normalize_tags

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BenchmarkPackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=20_000)
    version: str = Field(min_length=1, max_length=100)
    source: str | None = Field(default=None, max_length=2048)

    @field_validator("name", "slug", "version")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value must not be blank.")
        return value

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        value = value.lower()
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("Slug must use lowercase words separated by single hyphens.")
        return value

    @field_validator("description", "source")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class BenchmarkPackTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_task_id: UUID
    order_index: int = Field(ge=0)
    difficulty: TaskDifficulty | None = None
    tags: list[str] = Field(default_factory=list, max_length=25)

    @field_validator("difficulty", mode="before")
    @classmethod
    def normalize_difficulty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_difficulty(value)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return normalize_tags(values)


class BenchmarkPackSummary(BaseModel):
    task_count: int
    ready_task_count: int
    repositories_represented: list[str]
    difficulty_distribution: dict[str, int]
    tags: list[str]


class BenchmarkPackTaskRead(BaseModel):
    benchmark_task_id: UUID
    order_index: int
    difficulty: str | None = None
    tags: list[str]
    created_at: datetime
    issue_number: int | None = None
    issue_title: str
    task_status: str
    repository: str


class BenchmarkPackRead(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    version: str
    source: str | None = None
    created_at: datetime
    summary: BenchmarkPackSummary


class BenchmarkPackDetail(BenchmarkPackRead):
    tasks: list[BenchmarkPackTaskRead]
