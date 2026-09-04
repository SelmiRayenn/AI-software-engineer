from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.test_phases import TEST_PHASE_POST_PATCH
from app.db.base import Base
from app.db.session import get_db
from app.evaluation import EvaluationRunNotCompleteError, EvaluationService
from app.main import app
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    Repository,
)
from app.models import TestResult as ResultRecord

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
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_perfect_file_localization(db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py", "src/b.py"],
        inspected_files=["src/a.py", "src/b.py"],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.file_localization_score == 1.0


def test_partial_file_localization(db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py", "src/b.py"],
        inspected_files=["src/a.py"],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.file_localization_score == 0.5


def test_no_file_localization(db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py", "src/b.py"],
        inspected_files=["README.md"],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.file_localization_score == 0.0


def test_unrelated_file_count(db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py"],
        generated_files=["src/a.py", "src/unrelated.py", "docs/notes.md"],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.modified_files_count == 3
    assert metric.unrelated_files_count == 2


def test_tests_passed_true_only_when_all_post_patch_tests_pass(db: Session) -> None:
    passing_run_id = create_completed_run(db, post_patch_passed=[True, True])
    failing_run_id = create_completed_run(db, post_patch_passed=[True, False])
    missing_run_id = create_completed_run(db, post_patch_passed=[])

    passing_metric = EvaluationService(db=db, agent_run_id=passing_run_id).evaluate()
    failing_metric = EvaluationService(db=db, agent_run_id=failing_run_id).evaluate()
    missing_metric = EvaluationService(db=db, agent_run_id=missing_run_id).evaluate()

    assert passing_metric.tests_passed is True
    assert failing_metric.tests_passed is False
    assert missing_metric.tests_passed is False


def test_patch_applied_requires_generated_patch_and_clean_application_event(db: Session) -> None:
    applied_run_id = create_completed_run(db, patch_applied=True)
    stored_only_run_id = create_completed_run(db, patch_applied=False)
    empty_patch_run_id = create_completed_run(db, generated_patch_text="", patch_applied=True)

    applied_metric = EvaluationService(db=db, agent_run_id=applied_run_id).evaluate()
    stored_only_metric = EvaluationService(db=db, agent_run_id=stored_only_run_id).evaluate()
    empty_patch_metric = EvaluationService(db=db, agent_run_id=empty_patch_run_id).evaluate()

    assert applied_metric.patch_applied is True
    assert stored_only_metric.patch_applied is False
    assert empty_patch_metric.patch_applied is False


def test_mock_token_and_cost_aggregation_returns_zero(db: Session) -> None:
    run_id = create_completed_run(
        db,
        model_provider="mock",
        model_events=[
            {
                "input_tokens": 500,
                "output_tokens": 200,
                "estimated_cost": 99.0,
            }
        ],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.tokens_used == 0
    assert metric.estimated_cost == 0.0


def test_non_mock_token_and_cost_aggregation(db: Session) -> None:
    run_id = create_completed_run(
        db,
        model_provider="openai",
        model_events=[
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "estimated_cost": 0.03,
            },
            {
                "input_tokens": 25,
                "output_tokens": 10,
                "estimated_cost": 0.01,
            },
        ],
    )

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.tokens_used == 185
    assert metric.estimated_cost == 0.04


def test_execution_time_uses_run_timestamps(db: Session) -> None:
    run_id = create_completed_run(db, execution_seconds=12.5)

    metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert metric.execution_time_seconds == 12.5


def test_re_evaluation_updates_existing_metric_instead_of_duplicating(db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py"],
        inspected_files=[],
    )

    first_metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()
    db.add(
        AgentEvent(
            agent_run_id=run_id,
            event_type="agent_tool_call",
            payload_json={"tool_name": "read_file", "files_read": ["src/a.py"]},
        )
    )
    db.commit()

    second_metric = EvaluationService(db=db, agent_run_id=run_id).evaluate()

    assert first_metric.id == second_metric.id
    assert second_metric.file_localization_score == 1.0
    assert db.scalar(select(func.count()).select_from(EvaluationMetric)) == 1


def test_evaluation_rejects_non_completed_run(db: Session) -> None:
    run_id = create_completed_run(db, status="running")

    try:
        EvaluationService(db=db, agent_run_id=run_id).evaluate()
    except EvaluationRunNotCompleteError as exc:
        assert "completed" in str(exc)
    else:
        raise AssertionError("Expected EvaluationRunNotCompleteError")


def test_metrics_api_evaluates_and_reads_metrics(client: TestClient, db: Session) -> None:
    run_id = create_completed_run(
        db,
        gold_files=["src/a.py"],
        inspected_files=["src/a.py"],
        generated_files=["src/a.py"],
        patch_applied=True,
    )

    evaluate_response = client.post(f"/agent-runs/{run_id}/evaluate")

    assert evaluate_response.status_code == 200
    assert evaluate_response.json()["file_localization_score"] == 1.0
    assert evaluate_response.json()["patch_applied"] is True

    metrics_response = client.get(f"/agent-runs/{run_id}/metrics")

    assert metrics_response.status_code == 200
    assert metrics_response.json()["id"] == evaluate_response.json()["id"]


def test_metrics_api_rejects_non_completed_run(client: TestClient, db: Session) -> None:
    run_id = create_completed_run(db, status="running")

    response = client.post(f"/agent-runs/{run_id}/evaluate")

    assert response.status_code == 409


def create_completed_run(
    db: Session,
    *,
    gold_files: list[str] | None = None,
    inspected_files: list[str] | None = None,
    generated_files: list[str] | None = None,
    generated_patch_text: str = "diff --git a/src/a.py b/src/a.py\n",
    patch_applied: bool = True,
    post_patch_passed: list[bool] | None = None,
    model_provider: str = "mock",
    model_events: list[dict[str, object]] | None = None,
    execution_seconds: float = 8.0,
    status: str = "completed",
) -> UUID:
    repository_key = uuid4().hex
    repository = Repository(
        name=f"calculator-{repository_key}",
        owner="example",
        url=f"https://github.com/example/calculator-{repository_key}",
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
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=gold_files or ["src/a.py"],
            patch_text="hidden gold patch",
            test_files=[],
        )
    )

    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=execution_seconds)
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider=model_provider,
        model_name="test-model",
        status=status,
        started_at=started_at,
        completed_at=completed_at if status == "completed" else None,
    )
    db.add(run)
    db.flush()

    if generated_files is None:
        generated_files = ["src/a.py"] if generated_patch_text else []
    db.add(
        GeneratedPatch(
            agent_run_id=run.id,
            patch_text=generated_patch_text,
            changed_files=generated_files,
        )
    )

    for file_path in inspected_files or []:
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_tool_call",
                payload_json={"tool_name": "read_file", "files_read": [file_path]},
            )
        )

    for payload in model_events or []:
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="model_response",
                payload_json=payload,
            )
        )

    if patch_applied:
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="test_phase_completed",
                payload_json={
                    "phase": TEST_PHASE_POST_PATCH,
                    "passed": True,
                    "patch_status": "applied",
                },
            )
        )

    for index, passed in enumerate([True] if post_patch_passed is None else post_patch_passed):
        db.add(
            ResultRecord(
                agent_run_id=run.id,
                phase=TEST_PHASE_POST_PATCH,
                command=f"pytest #{index}",
                passed=passed,
                exit_code=0 if passed else 1,
                stdout="",
                stderr="",
                duration_seconds=0.1,
            )
        )

    db.commit()
    return run.id
