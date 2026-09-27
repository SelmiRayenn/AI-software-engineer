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
    changed_hunk_count: int
    added_removed_ratio: float
    file_kind_counts: dict[str, int]
    duplicate_edit_count: int
    formatting_only_hunk_count: int
    unrelated_formatting_hunk_count: int
    large_rewrite_hunk_count: int
    generated_block_count: int
    uninspected_files: list[str] = Field(default_factory=list)
    minimization_score: float = Field(ge=0.0, le=1.0)
    minimization_warnings: list[str] = Field(default_factory=list)
    minimization_penalties: dict[str, float] = Field(default_factory=dict)
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
    code_quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    review_ready: bool | None = None
    review_blockers: list[str] = Field(default_factory=list)
    review_warnings: list[str] = Field(default_factory=list)
    max_patch_files: int
    max_patch_changed_lines: int
    created_at: datetime
