from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class GoldPatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "gold_patches"

    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    changed_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    test_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="gold_patch")
