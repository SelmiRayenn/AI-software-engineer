from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class BenchmarkPack(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "benchmark_packs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    task_memberships: Mapped[list["BenchmarkPackTask"]] = relationship(
        back_populates="benchmark_pack",
        cascade="all, delete-orphan",
        order_by="BenchmarkPackTask.order_index",
    )


class BenchmarkPackTask(CreatedAtMixin, Base):
    __tablename__ = "benchmark_pack_tasks"
    __table_args__ = (
        UniqueConstraint(
            "benchmark_pack_id", "order_index", name="uq_benchmark_pack_tasks_pack_order"
        ),
    )

    benchmark_pack_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_packs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    benchmark_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    benchmark_pack: Mapped["BenchmarkPack"] = relationship(back_populates="task_memberships")
    benchmark_task: Mapped["BenchmarkTask"] = relationship(back_populates="pack_memberships")
