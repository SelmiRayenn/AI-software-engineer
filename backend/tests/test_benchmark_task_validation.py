from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import BenchmarkTask, GoldPatch, Repository

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


def test_benchmark_task_validation_success(client: TestClient) -> None:
    task_id = create_task(with_gold=True)

    response = client.post(f"/benchmark-tasks/{task_id}/validate")

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["errors"] == []


def test_mark_ready_succeeds_after_validation(client: TestClient) -> None:
    task_id = create_task(with_gold=True)

    response = client.post(f"/benchmark-tasks/{task_id}/mark-ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"

    with TestingSessionLocal() as db:
        task = db.get(BenchmarkTask, UUID(task_id))
        assert task is not None
        assert task.status == "ready"


def test_benchmark_task_validation_failure(client: TestClient) -> None:
    task_id = create_task(
        repository_url="https://example.com/not-github",
        issue_number=0,
        pull_request_number=None,
        base_commit="",
        setup_commands="python -m pip install -e .",
        test_commands=[],
        with_gold=False,
    )

    response = client.post(f"/benchmark-tasks/{task_id}/validate")

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert "setup_commands must be a list." in payload["errors"]
    assert "test_commands must include at least one command." in payload["errors"]
    assert "GoldPatch must exist for evaluation." in payload["errors"]


def test_mark_ready_rejects_invalid_status_transition(client: TestClient) -> None:
    task_id = create_task(status="completed", with_gold=True)

    response = client.post(f"/benchmark-tasks/{task_id}/mark-ready")

    assert response.status_code == 409
    assert "Cannot transition benchmark task" in response.json()["detail"]


def test_mark_ready_is_blocked_when_task_is_invalid(client: TestClient) -> None:
    task_id = create_task(base_commit="", with_gold=False)

    response = client.post(f"/benchmark-tasks/{task_id}/mark-ready")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["valid"] is False
    assert "base_commit must be present." in detail["errors"]
    assert "GoldPatch must exist for evaluation." in detail["errors"]

    with TestingSessionLocal() as db:
        task = db.get(BenchmarkTask, UUID(task_id))
        assert task is not None
        assert task.status == "draft"


def create_task(
    *,
    repository_url: str = "https://github.com/example/calculator",
    issue_number: int = 42,
    pull_request_number: int | None = 43,
    base_commit: str = "1111111111111111111111111111111111111111",
    setup_commands=None,
    test_commands=None,
    status: str = "draft",
    with_gold: bool,
) -> str:
    with TestingSessionLocal() as db:
        repository = Repository(
            name="calculator",
            owner="example",
            url=repository_url,
            default_branch="main",
            language="Python",
        )
        db.add(repository)
        db.flush()

        task = BenchmarkTask(
            repository_id=repository.id,
            issue_number=issue_number,
            issue_title="Validate benchmark task",
            issue_body="A small task for validation tests.",
            issue_comments=[],
            pull_request_number=pull_request_number,
            base_commit=base_commit,
            setup_commands=[] if setup_commands is None else setup_commands,
            test_commands=["pytest"] if test_commands is None else test_commands,
            status=status,
        )
        db.add(task)
        db.flush()

        if with_gold:
            db.add(
                GoldPatch(
                    benchmark_task_id=task.id,
                    changed_files=["calculator/core.py"],
                    patch_text="diff --git a/calculator/core.py b/calculator/core.py\n",
                    test_files=["tests/test_core.py"],
                )
            )

        db.commit()
        return str(task.id)
