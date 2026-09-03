from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BenchmarkTaskBase(BaseModel):
    repository_id: UUID
    issue_number: int
    issue_title: str
    issue_body: str | None = None
    base_commit: str
    fix_commit: str | None = None
    linked_pr_url: str | None = None
    status: str = "draft"


class BenchmarkTaskCreate(BenchmarkTaskBase):
    pass


class BenchmarkTaskRead(BenchmarkTaskBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
