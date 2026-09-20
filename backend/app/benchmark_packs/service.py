from __future__ import annotations

from collections import Counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import BenchmarkPack, BenchmarkPackTask, BenchmarkTask
from app.schemas.benchmark_pack import (
    BenchmarkPackCreate,
    BenchmarkPackDetail,
    BenchmarkPackRead,
    BenchmarkPackSummary,
    BenchmarkPackTaskCreate,
    BenchmarkPackTaskRead,
)


class BenchmarkPackNotFound(RuntimeError):
    pass


class BenchmarkTaskNotFound(RuntimeError):
    pass


class BenchmarkPackConflict(RuntimeError):
    pass


class BenchmarkPackService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def create_pack(self, data: BenchmarkPackCreate) -> BenchmarkPackRead:
        pack = BenchmarkPack(**data.model_dump())
        self._db.add(pack)
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            raise BenchmarkPackConflict("A benchmark pack with this slug already exists.") from exc
        return self.get_pack(pack.id, include_tasks=False)

    def list_packs(self, *, skip: int = 0, limit: int = 100) -> list[BenchmarkPackRead]:
        packs = list(
            self._db.scalars(
                self._pack_query()
                .order_by(BenchmarkPack.created_at, BenchmarkPack.id)
                .offset(skip)
                .limit(limit)
            )
        )
        return [self._to_read(pack) for pack in packs]

    def get_pack(
        self, pack_id: UUID, *, include_tasks: bool = True
    ) -> BenchmarkPackRead | BenchmarkPackDetail:
        pack = self._db.scalar(self._pack_query().where(BenchmarkPack.id == pack_id))
        if pack is None:
            raise BenchmarkPackNotFound("Benchmark pack not found.")
        return self._to_detail(pack) if include_tasks else self._to_read(pack)

    def add_task(self, pack_id: UUID, data: BenchmarkPackTaskCreate) -> BenchmarkPackTaskRead:
        if self._db.get(BenchmarkPack, pack_id) is None:
            raise BenchmarkPackNotFound("Benchmark pack not found.")
        task = self._db.get(BenchmarkTask, data.benchmark_task_id)
        if task is None:
            raise BenchmarkTaskNotFound("Benchmark task not found.")
        membership = BenchmarkPackTask(
            benchmark_pack_id=pack_id,
            benchmark_task_id=data.benchmark_task_id,
            order_index=data.order_index,
            difficulty=data.difficulty,
            tags=data.tags,
        )
        self._db.add(membership)
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            duplicate = self._db.get(BenchmarkPackTask, (pack_id, data.benchmark_task_id))
            message = (
                "Benchmark task is already in this pack."
                if duplicate is not None
                else "Order index is already used in this pack."
            )
            raise BenchmarkPackConflict(message) from exc
        self._db.refresh(membership)
        return _membership_read(membership, task)

    def remove_task(self, pack_id: UUID, task_id: UUID) -> None:
        if self._db.get(BenchmarkPack, pack_id) is None:
            raise BenchmarkPackNotFound("Benchmark pack not found.")
        membership = self._db.get(BenchmarkPackTask, (pack_id, task_id))
        if membership is None:
            raise BenchmarkTaskNotFound("Benchmark task is not in this pack.")
        self._db.delete(membership)
        self._db.commit()

    def _pack_query(self):
        return select(BenchmarkPack).options(
            selectinload(BenchmarkPack.task_memberships)
            .selectinload(BenchmarkPackTask.benchmark_task)
            .selectinload(BenchmarkTask.repository)
        )

    def _to_read(self, pack: BenchmarkPack) -> BenchmarkPackRead:
        return BenchmarkPackRead(
            id=pack.id,
            name=pack.name,
            slug=pack.slug,
            description=pack.description,
            version=pack.version,
            source=pack.source,
            created_at=pack.created_at,
            summary=_pack_summary(pack.task_memberships),
        )

    def _to_detail(self, pack: BenchmarkPack) -> BenchmarkPackDetail:
        return BenchmarkPackDetail(
            **self._to_read(pack).model_dump(),
            tasks=[
                _membership_read(membership, membership.benchmark_task)
                for membership in sorted(
                    pack.task_memberships,
                    key=lambda item: (item.order_index, str(item.benchmark_task_id)),
                )
            ],
        )


def _pack_summary(memberships: list[BenchmarkPackTask]) -> BenchmarkPackSummary:
    difficulty = Counter(item.difficulty or "unspecified" for item in memberships)
    repositories = sorted(
        {
            f"{item.benchmark_task.repository.owner}/{item.benchmark_task.repository.name}"
            for item in memberships
        }
    )
    return BenchmarkPackSummary(
        task_count=len(memberships),
        ready_task_count=sum(item.benchmark_task.status == "ready" for item in memberships),
        repositories_represented=repositories,
        difficulty_distribution=dict(sorted(difficulty.items())),
        tags=sorted({tag for item in memberships for tag in item.tags}),
    )


def _membership_read(membership: BenchmarkPackTask, task: BenchmarkTask) -> BenchmarkPackTaskRead:
    return BenchmarkPackTaskRead(
        benchmark_task_id=task.id,
        order_index=membership.order_index,
        difficulty=membership.difficulty,
        tags=membership.tags,
        created_at=membership.created_at,
        issue_number=task.issue_number,
        issue_title=task.issue_title,
        task_status=task.status,
        repository=f"{task.repository.owner}/{task.repository.name}",
    )
