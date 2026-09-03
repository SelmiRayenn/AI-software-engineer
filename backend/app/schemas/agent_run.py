from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AgentRunBase(BaseModel):
    benchmark_task_id: UUID
    model_provider: str
    model_name: str
    status: str = "queued"


class AgentRunCreate(AgentRunBase):
    pass


class AgentRunRead(AgentRunBase):
    id: UUID
    started_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
