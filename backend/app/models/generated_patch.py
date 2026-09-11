from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun
    from app.models.human_review import HumanReview
    from app.models.patch_quality import PatchQuality


class GeneratedPatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "generated_patches"
    __table_args__ = (UniqueConstraint("agent_run_id", "version", name="uq_patch_run_version"),)

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    changed_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    is_selected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    agent_run: Mapped["AgentRun"] = relationship(back_populates="generated_patches")
    human_review: Mapped["HumanReview | None"] = relationship(
        back_populates="generated_patch",
        cascade="all, delete-orphan",
        uselist=False,
    )
    quality: Mapped["PatchQuality | None"] = relationship(
        back_populates="generated_patch",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def review_status(self) -> str:
        if self.human_review is None:
            return "pending"
        return self.human_review.decision
