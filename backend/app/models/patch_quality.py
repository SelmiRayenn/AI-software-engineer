from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.generated_patch import GeneratedPatch


class PatchQuality(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "patch_qualities"

    generated_patch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generated_patches.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    changed_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    added_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    removed_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    total_changed_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    max_patch_files: Mapped[int] = mapped_column(Integer, nullable=False)
    max_patch_changed_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_source_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    changed_test_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    changed_docs_config_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    suspicious_generated_files: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    unrelated_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    whitespace_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dependency_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    lockfiles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    hard_limit_violations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    generated_patch: Mapped["GeneratedPatch"] = relationship(back_populates="quality")
