from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TestResultBase(BaseModel):
    agent_run_id: UUID
    phase: str
    command: str
    passed: bool
    exit_code: int
    stdout: str | None = None
    stderr: str | None = None
    duration_seconds: float | None = None


class TestResultCreate(TestResultBase):
    pass


class TestResultRead(TestResultBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
