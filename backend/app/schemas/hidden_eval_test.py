from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.test_execution.hidden_workspace import validate_payload_path


class HiddenEvalTestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=255)
    commands: list[str] = Field(min_length=1, max_length=50)
    files_payload: dict[str, str] | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("Hidden test name must not be blank.")
        return name.strip()

    @field_validator("commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        normalized = [command.strip() for command in commands]
        if any(not command or "\x00" in command or len(command) > 16_384 for command in normalized):
            raise ValueError(
                "Hidden commands must be nonblank, NUL-free, and at most 16,384 characters."
            )
        return normalized

    @field_validator("files_payload")
    @classmethod
    def validate_files_payload(cls, files: dict[str, str] | None) -> dict[str, str] | None:
        if files is None:
            return None
        if len(files) > 50:
            raise ValueError("Hidden test files payload may contain at most 50 files.")
        if any(not path.strip() or not isinstance(content, str) for path, content in files.items()):
            raise ValueError("Hidden test file paths and contents must be non-empty strings.")
        if sum(len(content.encode("utf-8")) for content in files.values()) > 1_000_000:
            raise ValueError("Hidden test files payload exceeds 1,000,000 bytes.")
        normalized = [validate_payload_path(path) for path in files]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Hidden file paths must be unique after normalization.")
        for path in normalized:
            if any(other.startswith(path + "/") for other in normalized):
                raise ValueError("Hidden file paths must not overlap directories.")
        return files


class HiddenEvalTestRead(BaseModel):
    id: UUID
    benchmark_task_id: UUID
    name: str
    commands: list[str]
    files_payload: dict[str, str] | None = None
    enabled: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
