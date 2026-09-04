from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_event import AgentEvent
    from app.models.benchmark_task import BenchmarkTask
    from app.models.evaluation_metric import EvaluationMetric
    from app.models.generated_patch import GeneratedPatch
    from app.models.test_result import TestResult


class AgentRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_runs"

    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    model_provider: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="agent_runs")
    events: Mapped[list["AgentEvent"]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
    )
    generated_patch: Mapped["GeneratedPatch | None"] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        uselist=False,
    )
    test_results: Mapped[list["TestResult"]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
    )
    evaluation_metric: Mapped["EvaluationMetric | None"] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def patch_review_status(self) -> str | None:
        if self.generated_patch is None:
            return None
        return self.generated_patch.review_status
