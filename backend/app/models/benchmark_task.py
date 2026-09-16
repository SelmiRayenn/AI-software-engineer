from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun
    from app.models.gold_patch import GoldPatch
    from app.models.hidden_eval_test import HiddenEvalTest
    from app.models.repository import Repository


class BenchmarkTask(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "benchmark_tasks"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_title: Mapped[str] = mapped_column(String(500), nullable=False)
    issue_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_comments: Mapped[list[dict[str, str | None]]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    fix_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_pr_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    setup_commands: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    test_commands: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_lockfile_changes: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    allow_dependency_file_changes: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), default="draft", index=True, nullable=False)

    repository: Mapped["Repository"] = relationship(back_populates="benchmark_tasks")
    gold_patch: Mapped["GoldPatch | None"] = relationship(
        back_populates="benchmark_task",
        cascade="all, delete-orphan",
        uselist=False,
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="benchmark_task",
        cascade="all, delete-orphan",
    )
    hidden_eval_tests: Mapped[list["HiddenEvalTest"]] = relationship(
        back_populates="benchmark_task",
        cascade="all, delete-orphan",
        order_by="HiddenEvalTest.created_at",
    )
