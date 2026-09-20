from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class BenchmarkImport(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Trusted provenance, deliberately absent from agent-facing read models."""

    __tablename__ = "benchmark_imports"

    task_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    environment_setup_commit: Mapped[str | None] = mapped_column(String(64))
    hints_text: Mapped[str | None] = mapped_column(Text)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="import_record")
