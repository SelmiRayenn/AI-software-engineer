from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_event import AgentEvent
    from app.models.agent_run_failure import AgentRunFailure
    from app.models.benchmark_task import BenchmarkTask
    from app.models.evaluation_metric import EvaluationMetric
    from app.models.generated_patch import GeneratedPatch
    from app.models.repository_index import RepositoryIndex
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
    repair_attempts_used: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    final_patch_passed_tests: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    generated_patches: Mapped[list["GeneratedPatch"]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        order_by="GeneratedPatch.version",
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
    repository_index: Mapped["RepositoryIndex | None"] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        uselist=False,
    )
    failure: Mapped["AgentRunFailure | None"] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def generated_patch(self) -> "GeneratedPatch | None":
        return max(
            self.generated_patches,
            key=lambda patch: (patch.is_selected, patch.version),
            default=None,
        )

    @property
    def final_patch_id(self) -> uuid.UUID | None:
        patch = self.generated_patch
        return patch.id if patch and patch.is_selected else None

    @property
    def patch_review_status(self) -> str | None:
        if self.generated_patch is None:
            return None
        return self.generated_patch.review_status

    @property
    def failure_category(self) -> str | None:
        return self.failure.category if self.failure else None
