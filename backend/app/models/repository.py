from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.benchmark_task import BenchmarkTask


class Repository(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repositories_owner_name"),)

    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    owner: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    language: Mapped[str | None] = mapped_column(String(100), nullable=True)

    benchmark_tasks: Mapped[list["BenchmarkTask"]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )
