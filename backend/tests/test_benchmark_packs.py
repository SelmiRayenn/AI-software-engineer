from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from alembic import command
from app.core.config import settings
from app.db.base import Base
from app.db.migrations import make_alembic_config
from app.db.session import get_db
from app.main import create_app
from app.models import BenchmarkPackTask, BenchmarkTask, Repository

OPERATOR_TOKEN = "pack-import-token"


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


def create_pack(client: TestClient, *, slug: str = "python-core") -> dict:
    response = client.post(
        "/benchmark-packs",
        json={
            "name": "Python Core",
            "slug": slug,
            "description": "Stable Python repair tasks.",
            "version": "2026.1",
            "source": "internal-curation",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_task(
    db: Session,
    *,
    owner: str,
    name: str,
    title: str,
    status: str = "draft",
) -> BenchmarkTask:
    repository = db.scalar(
        select(Repository).where(Repository.owner == owner, Repository.name == name)
    )
    if repository is None:
        repository = Repository(owner=owner, name=name, url=f"https://github.com/{owner}/{name}")
        db.add(repository)
        db.flush()
    task = BenchmarkTask(
        repository_id=repository.id,
        issue_number=1,
        issue_title=title,
        base_commit="a" * 40,
        status=status,
    )
    db.add(task)
    db.commit()
    return task


def add_task(
    client: TestClient,
    pack_id: str,
    task_id,
    order_index: int,
    *,
    difficulty: str | None = None,
    tags: list[str] | None = None,
):
    return client.post(
        f"/benchmark-packs/{pack_id}/tasks",
        json={
            "benchmark_task_id": str(task_id),
            "order_index": order_index,
            "difficulty": difficulty,
            "tags": tags or [],
        },
    )


def test_create_list_and_get_pack(client: TestClient) -> None:
    created = create_pack(client)
    assert created["slug"] == "python-core"
    assert created["version"] == "2026.1"
    assert created["summary"] == {
        "task_count": 0,
        "ready_task_count": 0,
        "repositories_represented": [],
        "difficulty_distribution": {},
        "tags": [],
    }
    listed = client.get("/benchmark-packs")
    detail = client.get(f"/benchmark-packs/{created['id']}")
    assert listed.status_code == 200 and listed.json()[0]["id"] == created["id"]
    assert detail.status_code == 200 and detail.json()["tasks"] == []


def test_duplicate_pack_slug_rejected(client: TestClient) -> None:
    create_pack(client)
    response = client.post(
        "/benchmark-packs",
        json={"name": "Duplicate", "slug": "PYTHON-CORE", "version": "2"},
    )
    assert response.status_code == 409
    assert "slug" in response.json()["detail"]


def test_add_remove_tasks_ordering_and_summary(client: TestClient, db: Session) -> None:
    pack = create_pack(client)
    first = create_task(db, owner="org", name="alpha", title="First", status="ready")
    second = create_task(db, owner="org", name="beta", title="Second")
    third = create_task(db, owner="org", name="alpha", title="Third", status="ready")

    assert (
        add_task(
            client, pack["id"], first.id, 20, difficulty="Hard", tags=["Parser", "python"]
        ).status_code
        == 201
    )
    assert (
        add_task(
            client, pack["id"], second.id, 5, difficulty="easy", tags=["Python", "api"]
        ).status_code
        == 201
    )
    assert add_task(client, pack["id"], third.id, 10).status_code == 201

    detail = client.get(f"/benchmark-packs/{pack['id']}").json()
    assert [task["benchmark_task_id"] for task in detail["tasks"]] == [
        str(second.id),
        str(third.id),
        str(first.id),
    ]
    assert detail["summary"] == {
        "task_count": 3,
        "ready_task_count": 2,
        "repositories_represented": ["org/alpha", "org/beta"],
        "difficulty_distribution": {"easy": 1, "hard": 1, "unspecified": 1},
        "tags": ["api", "parser", "python"],
    }
    assert "patch_text" not in json.dumps(detail)

    removed = client.delete(f"/benchmark-packs/{pack['id']}/tasks/{third.id}")
    assert removed.status_code == 204
    detail = client.get(f"/benchmark-packs/{pack['id']}").json()
    assert [task["order_index"] for task in detail["tasks"]] == [5, 20]
    assert detail["summary"]["task_count"] == 2


def test_duplicate_task_and_order_index_rejected(client: TestClient, db: Session) -> None:
    pack = create_pack(client)
    first = create_task(db, owner="org", name="repo", title="First")
    second = create_task(db, owner="org", name="repo", title="Second")
    assert add_task(client, pack["id"], first.id, 0).status_code == 201

    duplicate_task = add_task(client, pack["id"], first.id, 1)
    duplicate_order = add_task(client, pack["id"], second.id, 0)
    assert duplicate_task.status_code == 409
    assert "already in" in duplicate_task.json()["detail"]
    assert duplicate_order.status_code == 409
    assert "Order index" in duplicate_order.json()["detail"]


def test_missing_pack_task_and_membership_return_404(client: TestClient, db: Session) -> None:
    pack = create_pack(client)
    task = create_task(db, owner="org", name="repo", title="Task")
    unknown = "11111111-1111-4111-8111-111111111111"
    assert client.get(f"/benchmark-packs/{unknown}").status_code == 404
    assert add_task(client, unknown, task.id, 0).status_code == 404
    assert add_task(client, pack["id"], UUID(unknown), 0).status_code == 404
    assert client.delete(f"/benchmark-packs/{pack['id']}/tasks/{task.id}").status_code == 404


def test_import_assigns_only_successful_new_tasks_to_pack_in_source_order(
    client: TestClient, db: Session
) -> None:
    pack = create_pack(client)
    existing = create_task(db, owner="org", name="existing", title="Existing")
    assert add_task(client, pack["id"], existing.id, 7).status_code == 201
    records = [
        {
            "task_id": "import-one",
            "repo": "org/one",
            "problem_statement": "One",
            "base_commit": "1" * 40,
        },
        {"task_id": "invalid-row", "repo": "org/two", "problem_statement": "Two"},
        {
            "task_id": "import-two",
            "repo": "org/two",
            "problem_statement": "Two",
            "base_commit": "2" * 40,
        },
    ]
    response = client.post(
        f"/benchmark-imports?pack_id={pack['id']}",
        content="\n".join(json.dumps(record) for record in records),
        headers={
            "X-Operator-Token": OPERATOR_TOKEN,
            "Content-Type": "application/x-ndjson",
        },
    )
    assert response.status_code == 200
    assert response.json()["imported_count"] == 2
    assert response.json()["failed_count"] == 1
    detail = client.get(f"/benchmark-packs/{pack['id']}").json()
    assert [task["order_index"] for task in detail["tasks"]] == [7, 8, 9]
    imported_ids = response.json()["created_task_ids"]
    assert [task["benchmark_task_id"] for task in detail["tasks"][1:]] == imported_ids
    assert all(task["difficulty"] is None and task["tags"] == [] for task in detail["tasks"][1:])
    assert db.scalars(
        select(BenchmarkPackTask).where(BenchmarkPackTask.benchmark_pack_id == UUID(pack["id"]))
    ).all()


def test_import_with_missing_pack_is_document_failure(client: TestClient, db: Session) -> None:
    unknown = "11111111-1111-4111-8111-111111111111"
    response = client.post(
        f"/benchmark-imports?pack_id={unknown}",
        json=[
            {
                "task_id": "not-created",
                "repo": "org/repo",
                "problem_statement": "No pack",
                "base_commit": "a" * 40,
            }
        ],
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["failed_count"] == 1
    assert response.json()["errors"][0]["code"] == "pack_not_found"
    assert db.scalar(select(BenchmarkTask)) is None


def test_pack_migration_roundtrip(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'packs.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {"benchmark_packs", "benchmark_pack_tasks"}.issubset(inspector.get_table_names())
    assert {"name", "slug", "description", "version", "source"}.issubset(
        {column["name"] for column in inspector.get_columns("benchmark_packs")}
    )
    assert {"benchmark_pack_id", "benchmark_task_id", "order_index", "difficulty", "tags"}.issubset(
        {column["name"] for column in inspector.get_columns("benchmark_pack_tasks")}
    )
    command.downgrade(config, "20260919_0009")
    assert "benchmark_packs" not in inspect(engine).get_table_names()
    engine.dispose()
