from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
