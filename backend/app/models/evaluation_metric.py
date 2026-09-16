from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, false
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun


class EvaluationMetric(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "evaluation_metrics"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    file_localization_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    patch_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tests_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    baseline_tests_passed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    post_patch_tests_passed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    hidden_tests_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hidden_tests_run_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    hidden_tests_failed_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    issue_resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    regression_detected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    issue_specific_score: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0", nullable=False
    )
    modified_files_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unrelated_files_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="evaluation_metric")
