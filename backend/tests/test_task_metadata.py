from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import BenchmarkPackTask, BenchmarkTask, Repository

OPERATOR_TOKEN = "task-metadata-token"


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def client(db: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(settings, "database_auto_create_tables", False)
    monkeypatch.setattr(settings, "trusted_operator_token", OPERATOR_TOKEN)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def create_task(db: Session, *, difficulty: str = "unknown", tags: list[str] | None = None):
    repository = db.scalar(select(Repository).where(Repository.url == "https://github.com/example/sample"))
    if repository is None:
        repository = Repository(
            name="sample", owner="example", url="https://github.com/example/sample"
        )
    task = BenchmarkTask(
        repository=repository,
        issue_number=12,
        issue_title="Fix metadata filtering",
        base_commit="a" * 40,
        difficulty=difficulty,
        tags=tags or [],
    )
    db.add(task)
    db.commit()
    return task


def test_update_task_metadata_normalizes_difficulty_and_tags(client: TestClient, db: Session) -> None:
    task = create_task(db)
    response = client.patch(
        f"/benchmark-tasks/{task.id}/metadata",
        json={"difficulty": " Hard ", "tags": ["Python", "api-fix", "python"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["difficulty"] == "hard"
    assert response.json()["tags"] == ["python", "api-fix"]
    db.expire_all()
    assert db.get(BenchmarkTask, task.id).tags == ["python", "api-fix"]


def test_invalid_metadata_is_rejected_and_missing_task_is_clear(client: TestClient, db: Session) -> None:
    task = create_task(db)
    assert client.patch(
        f"/benchmark-tasks/{task.id}/metadata", json={"difficulty": "impossible"}
    ).status_code == 422
    assert client.patch(
        f"/benchmark-tasks/{task.id}/metadata", json={"tags": ["not a slug"]}
    ).status_code == 422
    assert client.patch(
        "/benchmark-tasks/11111111-1111-4111-8111-111111111111/metadata", json={"tags": []}
    ).status_code == 404


def test_task_filters_and_tag_catalog(client: TestClient, db: Session) -> None:
    create_task(db, difficulty="easy", tags=["python"])
    create_task(db, difficulty="hard", tags=["python", "api"])
    create_task(db, difficulty="hard", tags=["docs"])
    response = client.get("/benchmark-tasks?difficulty=hard&tag=python")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["difficulty"] == "hard"
    assert response.json()[0]["tags"] == ["python", "api"]
    assert client.get("/api/v1/benchmark-tasks?tag=api").json()[0]["difficulty"] == "hard"
    assert client.get("/benchmark-tasks/tags").json() == ["api", "docs", "python"]


def test_import_populates_task_and_pack_metadata(client: TestClient, db: Session) -> None:
    pack = client.post(
        "/benchmark-packs",
        json={"name": "Metadata Pack", "slug": "metadata-pack", "version": "1"},
    ).json()
    response = client.post(
        f"/benchmark-imports?pack_id={pack['id']}",
        json=[
            {
                "task_id": "metadata-import-1",
                "repo": "example/imported",
                "problem_statement": "Imported metadata task",
                "base_commit": "b" * 40,
                "difficulty": "medium",
                "tags": ["Python", "import"],
            }
        ],
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert response.status_code == 200, response.text
    task_id = UUID(response.json()["created_task_ids"][0])
    task = db.get(BenchmarkTask, task_id)
    membership = db.scalar(select(BenchmarkPackTask).where(BenchmarkPackTask.benchmark_task_id == task_id))
    assert task.difficulty == "medium" and task.tags == ["python", "import"]
    assert membership is not None
    assert membership.difficulty == "medium" and membership.tags == ["python", "import"]
    assert client.get(f"/benchmark-packs/{pack['id']}").json()["tasks"][0]["tags"] == [
        "python",
        "import",
    ]
