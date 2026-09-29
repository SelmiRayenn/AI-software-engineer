from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.redaction import redact_structured_value


class AgentEventBase(BaseModel):
    agent_run_id: UUID
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict)


class AgentEventCreate(AgentEventBase):
    pass


class AgentEventRead(AgentEventBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("payload_json", mode="before")
    @classmethod
    def redact_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return redact_structured_value(value)
