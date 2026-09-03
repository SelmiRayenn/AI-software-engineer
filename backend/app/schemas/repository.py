from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RepositoryBase(BaseModel):
    name: str
    owner: str
    url: str
    default_branch: str = "main"
    language: str | None = None


class RepositoryCreate(RepositoryBase):
    pass


class RepositoryRead(RepositoryBase):
    id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
