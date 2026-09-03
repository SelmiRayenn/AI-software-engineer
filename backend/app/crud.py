from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, BenchmarkTask, GoldPatch, Repository
from app.schemas.agent_run import AgentRunCreate
from app.schemas.benchmark_task import BenchmarkTaskCreate
from app.schemas.repository import RepositoryCreate


def create_repository(db: Session, repository_in: RepositoryCreate) -> Repository:
    repository = Repository(**repository_in.model_dump())
    db.add(repository)
    db.commit()
    db.refresh(repository)
    return repository


def get_repository(db: Session, repository_id: UUID) -> Repository | None:
    return db.get(Repository, repository_id)


def get_repository_by_owner_name(db: Session, owner: str, name: str) -> Repository | None:
    statement = select(Repository).where(Repository.owner == owner, Repository.name == name)
    return db.scalar(statement)


def list_repositories(db: Session, skip: int = 0, limit: int = 100) -> list[Repository]:
    statement = select(Repository).order_by(Repository.created_at.desc()).offset(skip).limit(limit)
    return list(db.scalars(statement).all())


def create_benchmark_task(db: Session, task_in: BenchmarkTaskCreate) -> BenchmarkTask:
    task = BenchmarkTask(**task_in.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_benchmark_task(db: Session, task_id: UUID) -> BenchmarkTask | None:
    return db.get(BenchmarkTask, task_id)


def get_gold_patch_for_task(db: Session, task_id: UUID) -> GoldPatch | None:
    statement = select(GoldPatch).where(GoldPatch.benchmark_task_id == task_id)
    return db.scalar(statement)


def list_benchmark_tasks(
    db: Session,
    repository_id: UUID | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[BenchmarkTask]:
    statement = select(BenchmarkTask).order_by(BenchmarkTask.created_at.desc())
    if repository_id is not None:
        statement = statement.where(BenchmarkTask.repository_id == repository_id)
    statement = statement.offset(skip).limit(limit)
    return list(db.scalars(statement).all())


def create_agent_run(db: Session, run_in: AgentRunCreate) -> AgentRun:
    run = AgentRun(**run_in.model_dump())
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def list_agent_runs(
    db: Session,
    benchmark_task_id: UUID | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[AgentRun]:
    statement = select(AgentRun).order_by(AgentRun.started_at.desc())
    if benchmark_task_id is not None:
        statement = statement.where(AgentRun.benchmark_task_id == benchmark_task_id)
    statement = statement.offset(skip).limit(limit)
    return list(db.scalars(statement).all())
