from typing import Literal

from pydantic import BaseModel, Field


class TargetedTestSelectionRequest(BaseModel):
    targeted_tests_max_commands: int | None = Field(default=None, ge=1, le=20, strict=True)
    targeted_tests_trusted_gold_files: bool = False


class TargetedTestSelectionRead(BaseModel):
    selected_commands: list[str] = Field(default_factory=list)
    selection_reason: str
    confidence: Literal["low", "medium", "high"]
    fallback_to_full_suite: bool
