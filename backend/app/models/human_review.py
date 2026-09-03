from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.generated_patch import GeneratedPatch


class HumanReview(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "human_reviews"

    generated_patch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generated_patches.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    reviewer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    generated_patch: Mapped["GeneratedPatch"] = relationship(back_populates="human_review")
