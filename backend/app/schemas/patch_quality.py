from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PatchQualityRead(BaseModel):
    patch_id: UUID
    accepted: bool
    changed_file_count: int
    added_lines: int
    removed_lines: int
    total_changed_lines: int
    changed_source_files: list[str] = Field(default_factory=list)
    changed_test_files: list[str] = Field(default_factory=list)
    changed_docs_config_files: list[str] = Field(default_factory=list)
    suspicious_generated_files: list[str] = Field(default_factory=list)
    unrelated_files_count: int
    whitespace_only: bool
    touches_dependency_files: bool
    touches_lockfiles: bool
    dependency_files: list[str] = Field(default_factory=list)
    lockfiles: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    hard_limit_violations: list[str] = Field(default_factory=list)
    max_patch_files: int
    max_patch_changed_lines: int
    created_at: datetime
