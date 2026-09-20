from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class HiddenEvalTest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "hidden_eval_tests"

    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    commands: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    files_payload: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    patch_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_metadata: Mapped[dict[str, list[str]] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )

    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="hidden_eval_tests")
