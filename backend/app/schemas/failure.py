from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.core.redaction import redact_common_secrets

FailureCategory = Literal[
    "task_not_ready",
    "repository_checkout_failed",
    "docker_unavailable",
    "setup_failed",
    "baseline_tests_failed",
    "model_provider_error",
    "malformed_tool_call",
    "unknown_tool",
    "tool_error_limit_reached",
    "patch_generation_failed",
    "patch_apply_failed",
    "patch_quality_blocked",
    "post_patch_tests_failed",
    "max_steps_reached",
    "max_repair_attempts_reached",
    "timeout",
    "cancelled",
    "unknown",
]


class AgentRunFailureRead(BaseModel):
    id: UUID
    agent_run_id: UUID
    category: FailureCategory
    human_readable_summary: str
    source_event_id: UUID | None = None
    created_at: datetime

    @field_validator("human_readable_summary", mode="before")
    @classmethod
    def redact_summary(cls, value: str) -> str:
        return redact_common_secrets(value)
