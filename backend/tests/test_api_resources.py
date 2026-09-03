from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db() -> Generator[Session, None, None]:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_repository_task_and_agent_run_create_list_flow(client: TestClient) -> None:
    repository_response = client.post(
        "/api/v1/repositories",
        json={
            "name": "calculator",
            "owner": "example",
            "url": "https://github.com/example/calculator",
            "default_branch": "main",
            "language": "Python",
        },
    )
    assert repository_response.status_code == 201
    repository_id = repository_response.json()["id"]

    task_response = client.post(
        "/api/v1/benchmark-tasks",
        json={
            "repository_id": repository_id,
            "issue_number": 42,
            "issue_title": "Fix division by zero handling",
            "issue_body": "Division by zero should return a clear validation error.",
            "base_commit": "1111111111111111111111111111111111111111",
            "fix_commit": "2222222222222222222222222222222222222222",
            "linked_pr_url": "https://github.com/example/calculator/pull/43",
            "status": "ready",
        },
    )
    assert task_response.status_code == 201
    task_id = task_response.json()["id"]

    run_response = client.post(
        "/api/v1/agent-runs",
        json={
            "benchmark_task_id": task_id,
            "model_provider": "openai",
            "model_name": "placeholder-model",
            "status": "queued",
        },
    )
    assert run_response.status_code == 201

    repositories = client.get("/api/v1/repositories")
    tasks = client.get("/api/v1/benchmark-tasks")
    runs = client.get("/api/v1/agent-runs")

    assert repositories.status_code == 200
    assert tasks.status_code == 200
    assert runs.status_code == 200
    assert len(repositories.json()) == 1
    assert len(tasks.json()) == 1
    assert len(runs.json()) == 1
