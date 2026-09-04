from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.test_result import TestResultRead


class TestExecutionRequest(BaseModel):
    command_timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class TestExecutionResponse(BaseModel):
    agent_run_id: UUID
    phase: str
    passed: bool
    setup_results: list[TestResultRead] = Field(default_factory=list)
    test_results: list[TestResultRead] = Field(default_factory=list)
    patch_status: str | None = None
    error_message: str | None = None
