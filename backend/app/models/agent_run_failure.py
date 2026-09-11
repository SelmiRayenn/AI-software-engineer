from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_event import AgentEvent
    from app.models.agent_run import AgentRun


class AgentRunFailure(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_run_failures"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    human_readable_summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent_run: Mapped["AgentRun"] = relationship(back_populates="failure")
    source_event: Mapped["AgentEvent | None"] = relationship()
