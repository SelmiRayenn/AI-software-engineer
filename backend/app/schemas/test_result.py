from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.redaction import redact_and_truncate


class TestResultBase(BaseModel):
    agent_run_id: UUID
    phase: str
    generated_patch_id: UUID | None = None
    attempt_number: int | None = None
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

    @field_validator("command", "stdout", "stderr", mode="before")
    @classmethod
    def redact_output(cls, value: str | None) -> str | None:
        return redact_and_truncate(value, max_chars=64_000) if value is not None else None
