import json
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentEvent,
    AgentRun,
    AgentRunFailure,
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    Repository,
)
from app.models.test_result import TestResult as StoredTestResult

engine = create_engine(
    "sqlite://",
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


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def create_run_with_trace(db: Session) -> AgentRun:
    repository = Repository(
        owner="example",
        name="trace",
        url="https://github.com/example/trace",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=7,
        issue_title="Fix trace behavior",
        issue_body="Public issue context",
        base_commit="a" * 40,
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=["secret/gold_fix.py"],
            patch_text="GOLD_PATCH_MUST_NOT_APPEAR",
            test_files=["tests/hidden_secret.py"],
        )
    )
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="trace-model",
        status="failed",
        started_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
    )
    db.add(run)
    db.flush()

    later = AgentEvent(
        agent_run_id=run.id,
        event_type="tool_call_failed",
        created_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        payload_json={
            "tool_name": "read_file",
            "success": False,
            "input": {"path": "src/app.py", "github_token": "ghp_123456789secret"},
            "files_read": ["src/app.py", ".gold/solution.py"],
            "api_key": "sk-123456789secret",
            "gold_patch": "EVENT_GOLD_SECRET",
            "output": "x" * 50_000,
        },
    )
    earlier = AgentEvent(
        agent_run_id=run.id,
        event_type="model_call_started",
        created_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        payload_json={"model_name": "trace-model"},
    )
    hidden = AgentEvent(
        agent_run_id=run.id,
        event_type="test_phase_completed",
        created_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
        payload_json={
            "phase": "hidden_eval",
            "passed": False,
            "command_count": 1,
            "command": "python tests/HIDDEN_COMMAND_SECRET.py",
            "stdout": "HIDDEN_OUTPUT_SECRET",
        },
    )
    db.add_all([later, earlier, hidden])
    db.flush()

    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text="diff --git a/src/app.py b/src/app.py\n",
        changed_files=["src/app.py", ".benchmark/gold/result.py"],
        version=1,
        is_selected=True,
    )
    db.add(patch)
    db.add_all(
        [
            StoredTestResult(
                agent_run_id=run.id,
                phase="baseline",
                command="pytest",
                passed=True,
                exit_code=0,
                duration_seconds=1.5,
            ),
            StoredTestResult(
                agent_run_id=run.id,
                phase="post_patch",
                command="pytest",
                passed=False,
                exit_code=1,
                duration_seconds=2.5,
            ),
            StoredTestResult(
                agent_run_id=run.id,
                phase="hidden_eval",
                command="python tests/HIDDEN_COMMAND_SECRET.py",
                passed=False,
                exit_code=1,
                stdout="HIDDEN_OUTPUT_SECRET",
                duration_seconds=3.0,
            ),
        ]
    )
    db.add(
        AgentRunFailure(
            agent_run_id=run.id,
            category="post_patch_tests_failed",
            human_readable_summary="Tests failed with api_key=sk-123456789secret",
            source_event_id=later.id,
        )
    )
    db.add(
        EvaluationMetric(
            agent_run_id=run.id,
            file_localization_score=0.5,
            patch_applied=True,
            tests_passed=False,
            baseline_tests_passed=True,
            post_patch_tests_passed=False,
            hidden_tests_passed=False,
            hidden_tests_run_count=1,
            hidden_tests_failed_count=1,
            issue_resolved=False,
            regression_detected=True,
            issue_specific_score=0.0,
            modified_files_count=1,
            unrelated_files_count=0,
            tokens_used=42,
            estimated_cost=0.01,
            execution_time_seconds=300.0,
        )
    )
    db.commit()
    return run


def test_trace_is_ordered_sanitized_and_includes_artifact_summaries(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        run = create_run_with_trace(db)
        run_id = run.id

    response = client.get(f"/agent-runs/{run_id}/trace")

    assert response.status_code == 200
    payload = response.json()
    assert [event["event_type"] for event in payload["events"]] == [
        "model_call_started",
        "tool_call_failed",
        "test_phase_completed",
    ]

    tool_event = payload["events"][1]
    assert tool_event["tool_name"] == "read_file"
    assert tool_event["file_paths"] == ["src/app.py"]
    assert tool_event["severity"] == "error"
    assert tool_event["sanitized_payload"]["api_key"] == "[REDACTED]"
    assert tool_event["sanitized_payload"]["input"]["github_token"] == "[REDACTED]"
    assert tool_event["sanitized_payload"]["output"]["truncated"] is True
    assert len(tool_event["sanitized_payload"]["output"]["preview"]) == 2_000

    hidden_event = payload["events"][2]
    assert hidden_event["sanitized_payload"] == {
        "phase": "hidden_eval",
        "passed": False,
        "command_count": 1,
    }
    assert hidden_event["file_paths"] == []

    assert payload["generated_patches"][0]["changed_files"] == ["src/app.py"]
    assert payload["generated_patches"][0]["is_selected"] is True
    assert payload["test_phases"] == [
        {
            "phase": "baseline",
            "command_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "duration_seconds": 1.5,
        },
        {
            "phase": "post_patch",
            "command_count": 1,
            "passed_count": 0,
            "failed_count": 1,
            "duration_seconds": 2.5,
        },
        {
            "phase": "hidden_eval",
            "command_count": 1,
            "passed_count": 0,
            "failed_count": 1,
            "duration_seconds": 3.0,
        },
    ]
    assert payload["failure"]["category"] == "post_patch_tests_failed"
    assert "[REDACTED]" in payload["failure"]["summary"]
    assert payload["metrics"]["file_localization_score"] == 0.5
    assert payload["metrics"]["regression_detected"] is True

    serialized = json.dumps(payload)
    for forbidden in (
        "GOLD_PATCH_MUST_NOT_APPEAR",
        "EVENT_GOLD_SECRET",
        "HIDDEN_COMMAND_SECRET",
        "HIDDEN_OUTPUT_SECRET",
        "ghp_123456789secret",
        "sk-123456789secret",
    ):
        assert forbidden not in serialized


def test_trace_missing_run_returns_404(client: TestClient) -> None:
    response = client.get("/agent-runs/11111111-1111-4111-8111-111111111111/trace")

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent run not found."


def test_trace_normalizes_nested_requested_tool_call(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        run = create_run_with_trace(db)
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="tool_call_requested",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                payload_json={
                    "tool_call": {
                        "id": "request-1",
                        "name": "read_file",
                        "arguments": {"path": "src/app.py"},
                    }
                },
            )
        )
        db.commit()
        run_id = run.id

    payload = client.get(f"/agent-runs/{run_id}/trace").json()
    requested = next(event for event in payload["events"] if event["event_type"] == "tool_call_requested")

    assert requested["tool_name"] == "read_file"
    assert requested["summary"] == "Tool requested: read_file"
    assert requested["file_paths"] == ["src/app.py"]
