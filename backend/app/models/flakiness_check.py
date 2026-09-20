from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class FlakinessCheck(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "flakiness_checks"

    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    repetitions_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    repetitions_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False)
    inconsistent_results: Mapped[bool] = mapped_column(Boolean, nullable=False)
    average_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    stop_on_first_failure: Mapped[bool] = mapped_column(Boolean, nullable=False)
    command_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_retained: Mapped[bool] = mapped_column(Boolean, nullable=False)
    setup_results: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="flakiness_checks")
    runs: Mapped[list["FlakinessCheckRun"]] = relationship(
        back_populates="flakiness_check",
        cascade="all, delete-orphan",
        order_by="FlakinessCheckRun.repetition_number",
    )


class FlakinessCheckRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "flakiness_check_runs"
    __table_args__ = (
        UniqueConstraint(
            "flakiness_check_id",
            "repetition_number",
            name="uq_flakiness_check_repetition",
        ),
    )

    flakiness_check_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("flakiness_checks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    repetition_number: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    command_results: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    flakiness_check: Mapped["FlakinessCheck"] = relationship(back_populates="runs")
