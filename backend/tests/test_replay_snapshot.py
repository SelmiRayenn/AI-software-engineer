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
    HiddenEvalTest,
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


def _create_replay_run(db: Session) -> AgentRun:
    repository = Repository(
        owner="example",
        name="replay",
        url="https://github.com/example/replay",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=60,
        issue_title="Fix replay snapshots",
        issue_body="Public issue body with api_key=issue-secret-value " + ("x" * 10_000),
        issue_comments=[{"body": "Inspect the public behavior", "user_login": "maintainer"}],
        base_commit="a" * 40,
        setup_commands=["python -m pip install -e ."],
        test_commands=["pytest -q"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=["src/gold_secret.py"],
            patch_text="GOLD_PATCH_PAYLOAD_MUST_NOT_APPEAR",
            test_files=["tests/hidden_gold_test.py"],
        )
    )
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name="private evaluator",
            commands=["pytest tests/HIDDEN_COMMAND_MUST_NOT_APPEAR.py"],
            patch_text="HIDDEN_TEST_PAYLOAD_MUST_NOT_APPEAR",
            enabled=True,
        )
    )
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="replay-model",
        status="failed",
        started_at=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 27, 10, 2, tzinfo=UTC),
    )
    db.add(run)
    db.flush()

    config = {
        "model_provider": "mock",
        "model_name": "replay-model",
        "max_steps": 8,
        "max_tool_errors": 3,
        "command_timeout_seconds": 120,
        "include_issue_comments": True,
        "enable_test_tool": True,
        "run_mode": "tool_loop",
    }
    prompt_preview = {
        "system_prompt": "System prompt",
        "developer_safety_prompt": "Safety " + ("s" * 10_000),
        "issue_context_prompt": "Issue context password=prompt-secret-value",
        "tool_use_instructions": "Allowed tools include read_file",
        "patch_submission_instructions": "Submit only through submit_patch",
    }
    db.add_all(
        [
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_run_configured",
                created_at=datetime(2026, 9, 27, 10, 0, 1, tzinfo=UTC),
                payload_json={"config": config, "prompt_preview": prompt_preview},
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="model_call_completed",
                created_at=datetime(2026, 9, 27, 10, 0, 2, tzinfo=UTC),
                payload_json={
                    "success": True,
                    "content_preview": "Use read_file with sk-model-secret-value",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "arguments": {"path": "src/app.py"},
                        }
                    ],
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "estimated_cost": 0.001,
                    "latency_seconds": 0.2,
                },
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="tool_call_requested",
                created_at=datetime(2026, 9, 27, 10, 0, 3, tzinfo=UTC),
                payload_json={
                    "tool_call": {
                        "id": "call-1",
                        "name": "read_file",
                        "arguments": {
                            "path": "src/app.py",
                            "api_key": "tool-secret-value",
                        },
                    }
                },
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="tool_call_completed",
                created_at=datetime(2026, 9, 27, 10, 0, 4, tzinfo=UTC),
                payload_json={
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "success": True,
                    "result": {
                        "content": "GOLD_EVENT_CONTENT_MUST_NOT_APPEAR" + ("y" * 20_000),
                        "path": "src/app.py",
                    },
                },
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="test_phase_completed",
                created_at=datetime(2026, 9, 27, 10, 1, tzinfo=UTC),
                payload_json={
                    "phase": "hidden_eval",
                    "passed": False,
                    "command_count": 1,
                    "command": "HIDDEN_COMMAND_MUST_NOT_APPEAR",
                    "stdout": "HIDDEN_OUTPUT_MUST_NOT_APPEAR",
                },
            ),
        ]
    )
    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text="PATCH_BODY_MUST_NOT_APPEAR",
        changed_files=["src/app.py"],
        version=1,
        is_selected=True,
    )
    db.add(patch)
    db.flush()
    db.add_all(
        [
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="post_patch",
                command="pytest -q",
                passed=False,
                exit_code=1,
                stdout="RAW_TEST_LOG_MUST_NOT_APPEAR",
                duration_seconds=2.0,
            ),
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="hidden_eval",
                command="HIDDEN_COMMAND_MUST_NOT_APPEAR",
                passed=False,
                exit_code=1,
                stderr="HIDDEN_OUTPUT_MUST_NOT_APPEAR",
                duration_seconds=1.0,
            ),
        ]
    )
    db.add(
        AgentRunFailure(
            agent_run_id=run.id,
            category="post_patch_tests_failed",
            human_readable_summary="Visible tests failed with token=run-secret-value",
        )
    )
    db.add(
        EvaluationMetric(
            agent_run_id=run.id,
            file_localization_score=1.0,
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
            tokens_used=120,
            estimated_cost=0.001,
            execution_time_seconds=120.0,
        )
    )
    db.commit()
    return run


def test_replay_snapshot_contains_reproducible_sanitized_context(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        run = _create_replay_run(db)
        run_id = run.id

    response = client.get(f"/agent-runs/{run_id}/replay-snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == str(run_id)
    assert payload["benchmark_context"]["issue_title"] == "Fix replay snapshots"
    assert payload["run_configuration"]["max_steps"] == 8
    assert payload["rendered_prompt_sections"]["system_prompt"]["text"] == "System prompt"
    assert "read_file" in payload["allowed_tools"]
    assert "run_tests" in payload["allowed_tools"]
    assert payload["tool_call_sequence"][0]["tool_name"] == "read_file"
    assert payload["tool_call_sequence"][0]["status"] == "completed"
    assert payload["tool_call_sequence"][0]["sanitized_input"]["api_key"] == "[REDACTED]"
    assert payload["model_response_summaries"][0]["requested_tools"] == ["read_file"]
    assert payload["patch_versions"][0]["version"] == 1
    assert payload["test_phases"][1]["phase"] == "hidden_eval"
    assert payload["failure_classification"]["category"] == "post_patch_tests_failed"
    assert payload["evaluation_metrics"]["tokens_used"] == 120
    assert payload["integrity"]["run_event_count"] == 5
    assert payload["integrity"]["patch_count"] == 1
    assert payload["integrity"]["test_result_count"] == 2
    assert len(payload["integrity"]["checksum_sha256"]) == 64


def test_replay_snapshot_excludes_protected_data_and_marks_safety_actions(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        run_id = _create_replay_run(db).id

    payload = client.get(f"/agent-runs/{run_id}/replay-snapshot").json()
    serialized = json.dumps(payload)

    for forbidden in (
        "GOLD_PATCH_PAYLOAD_MUST_NOT_APPEAR",
        "HIDDEN_TEST_PAYLOAD_MUST_NOT_APPEAR",
        "HIDDEN_COMMAND_MUST_NOT_APPEAR",
        "HIDDEN_OUTPUT_MUST_NOT_APPEAR",
        "PATCH_BODY_MUST_NOT_APPEAR",
        "RAW_TEST_LOG_MUST_NOT_APPEAR",
        "GOLD_EVENT_CONTENT_MUST_NOT_APPEAR",
        "issue-secret-value",
        "prompt-secret-value",
        "tool-secret-value",
        "model-secret-value",
        "run-secret-value",
    ):
        assert forbidden not in serialized
    assert payload["integrity"]["redaction_applied"] is True
    assert payload["integrity"]["truncation_applied"] is True
    assert payload["benchmark_context"]["issue_body"]["truncated"] is True
    assert payload["rendered_prompt_sections"]["developer_safety_prompt"]["truncated"] is True


def test_replay_snapshot_checksum_is_stable_for_unchanged_run(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        run_id = _create_replay_run(db).id

    first = client.get(f"/agent-runs/{run_id}/replay-snapshot").json()
    second = client.get(f"/agent-runs/{run_id}/replay-snapshot").json()

    assert first["integrity"]["checksum_sha256"] == second["integrity"]["checksum_sha256"]


def test_replay_snapshot_markdown_export(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        run_id = _create_replay_run(db).id

    response = client.get(f"/agent-runs/{run_id}/replay-snapshot?format=md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Agent Run Replay Snapshot" in response.text
    assert "## Tool Call Sequence" in response.text
    assert "## Evaluation Metrics" in response.text
    assert "GOLD_PATCH_PAYLOAD_MUST_NOT_APPEAR" not in response.text
    assert "HIDDEN_COMMAND_MUST_NOT_APPEAR" not in response.text


def test_replay_snapshot_missing_run_returns_404(client: TestClient) -> None:
    response = client.get("/agent-runs/11111111-1111-4111-8111-111111111111/replay-snapshot")

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent run not found."
