from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class BenchmarkPackRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "benchmark_pack_runs"

    benchmark_pack_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_packs.id", ondelete="RESTRICT"), index=True
    )
    pack_name: Mapped[str] = mapped_column(String(255))
    pack_slug: Mapped[str] = mapped_column(String(255))
    pack_version: Mapped[str] = mapped_column(String(100))
    model_provider: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(255))
    run_config: Mapped[dict] = mapped_column(JSON)
    include_hidden_tests: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_on_task_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(50), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aggregates: Mapped[dict] = mapped_column(JSON, default=dict)

    tasks: Mapped[list["BenchmarkPackRunTask"]] = relationship(
        back_populates="pack_run",
        cascade="all, delete-orphan",
        order_by="BenchmarkPackRunTask.order_index",
    )


class BenchmarkPackRunTask(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "benchmark_pack_run_tasks"
    __table_args__ = (
        UniqueConstraint(
            "benchmark_pack_run_id", "benchmark_task_id", name="uq_pack_run_tasks_task"
        ),
        UniqueConstraint("benchmark_pack_run_id", "order_index", name="uq_pack_run_tasks_order"),
    )

    benchmark_pack_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_pack_runs.id", ondelete="CASCADE"), index=True
    )
    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_tasks.id", ondelete="RESTRICT")
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), unique=True
    )
    order_index: Mapped[int] = mapped_column(Integer)
    task_snapshot: Mapped[dict] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(50), default="queued")
    metric_summary: Mapped[dict | None] = mapped_column(JSON)
    failure_category: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pack_run: Mapped["BenchmarkPackRun"] = relationship(back_populates="tasks")
