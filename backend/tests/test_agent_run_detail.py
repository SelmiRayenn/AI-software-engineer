from __future__ import annotations

from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentRun,
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    HumanReview,
    Repository,
)

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
def db() -> Generator[Session, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_existing_run_returns_detail(client: TestClient, db: Session) -> None:
    run_id = create_run(db)

    response = client.get(f"/agent-runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(run_id)
    assert payload["status"] == "completed"
    assert payload["benchmark_task"]["issue_title"] == "Fix calculator"
    assert payload["benchmark_task"]["issue_number"] == 42
    assert payload["repository"] == {
        "name": "calculator",
        "owner": "example",
        "url": "https://github.com/example/calculator",
    }
    assert payload["model_provider"] == "mock"
    assert payload["model_name"] == "scripted-mock"
    assert payload["review_status"] is None
    assert payload["changed_files"] == []
    assert payload["metric_summary"] is None


def test_missing_run_returns_404(client: TestClient) -> None:
    response = client.get(f"/agent-runs/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent run not found"


def test_patch_and_review_data_included_when_present(client: TestClient, db: Session) -> None:
    run_id = create_run(db, with_generated_patch=True, with_review=True)

    response = client.get(f"/agent-runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "approved"
    assert payload["changed_files"] == ["src/calculator.py"]


def test_metrics_included_when_present(client: TestClient, db: Session) -> None:
    run_id = create_run(db, with_generated_patch=True, with_metrics=True)

    response = client.get(f"/agent-runs/{run_id}")

    assert response.status_code == 200
    metrics = response.json()["metric_summary"]
    assert metrics["tests_passed"] is True
    assert metrics["patch_applied"] is True
    assert metrics["file_localization_score"] == 0.75
    assert metrics["modified_files_count"] == 1
    assert metrics["unrelated_files_count"] == 0
    assert metrics["tokens_used"] == 123
    assert metrics["estimated_cost"] == 0.05
    assert metrics["execution_time_seconds"] == 4.2


def test_gold_patch_data_is_not_exposed(client: TestClient, db: Session) -> None:
    run_id = create_run(db, with_generated_patch=True, with_metrics=True, with_gold_patch=True)

    response = client.get(f"/agent-runs/{run_id}")

    assert response.status_code == 200
    payload_text = response.text
    assert "secret_gold_solution.py" not in payload_text
    assert "hidden gold diff content" not in payload_text
    assert "gold" not in response.json()


def create_run(
    db: Session,
    *,
    with_generated_patch: bool = False,
    with_review: bool = False,
    with_metrics: bool = False,
    with_gold_patch: bool = False,
) -> UUID:
    repository = Repository(
        name="calculator",
        owner="example",
        url="https://github.com/example/calculator",
        default_branch="main",
        language="Python",
    )
    db.add(repository)
    db.flush()

    task = BenchmarkTask(
        repository_id=repository.id,
        issue_number=42,
        issue_title="Fix calculator",
        issue_body="Calculator should work.",
        issue_comments=[],
        pull_request_number=43,
        base_commit="1111111111111111111111111111111111111111",
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()

    if with_gold_patch:
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=["secret_gold_solution.py"],
                patch_text="hidden gold diff content",
                test_files=["tests/test_secret_gold_solution.py"],
            )
        )

    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="scripted-mock",
        status="completed",
    )
    db.add(run)
    db.flush()

    if with_generated_patch:
        generated_patch = GeneratedPatch(
            agent_run_id=run.id,
            patch_text="diff --git a/src/calculator.py b/src/calculator.py\n",
            changed_files=["src/calculator.py"],
        )
        db.add(generated_patch)
        db.flush()

        if with_review:
            db.add(
                HumanReview(
                    generated_patch_id=generated_patch.id,
                    decision="approved",
                    reviewer_name="Ada",
                    review_notes="Looks correct.",
                )
            )

    if with_metrics:
        db.add(
            EvaluationMetric(
                agent_run_id=run.id,
                file_localization_score=0.75,
                patch_applied=True,
                tests_passed=True,
                modified_files_count=1,
                unrelated_files_count=0,
                tokens_used=123,
                estimated_cost=0.05,
                execution_time_seconds=4.2,
            )
        )

    db.commit()
    return run.id
