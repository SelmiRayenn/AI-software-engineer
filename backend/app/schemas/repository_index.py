from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RepositoryIndexRead(BaseModel):
    id: UUID
    agent_run_id: UUID
    workspace_id: str
    index_version: int
    indexed_at: datetime
    created_at: datetime
    file_count: int
    total_size_bytes: int
    checksum: str
    skipped_counts: dict[str, int]

    model_config = ConfigDict(from_attributes=True)


class IndexedSymbolRead(BaseModel):
    id: UUID
    name: str
    kind: str
    line_number: int
    end_line_number: int

    model_config = ConfigDict(from_attributes=True)


class IndexedFileRead(BaseModel):
    id: UUID
    repository_index_id: UUID
    file_path: str
    extension: str
    size_bytes: int
    language: str
    file_type: str
    preview: str
    checksum: str
    imports: list[str]
    python_parse_error: bool
    symbols: list[IndexedSymbolRead]

    model_config = ConfigDict(from_attributes=True)
