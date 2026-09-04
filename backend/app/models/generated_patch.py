from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun
    from app.models.human_review import HumanReview


class GeneratedPatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "generated_patches"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    changed_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="generated_patch")
    human_review: Mapped["HumanReview | None"] = relationship(
        back_populates="generated_patch",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def review_status(self) -> str:
        if self.human_review is None:
            return "pending"
        return self.human_review.decision
