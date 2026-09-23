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
    HumanReview,
    PatchQuality,
    Repository,
)
from app.models.test_result import TestResult as StoredTestResult
from app.run_reports.service import MAX_DIFF_BYTES, MAX_LOG_BYTES

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


def create_report_run(db: Session, *, decision: str = "approved") -> AgentRun:
    repository = Repository(
        owner="example",
        name="reporting",
        url="https://github.com/example/reporting",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=46,
        issue_title="Fix report generation",
        issue_body="Reports must safely summarize agent runs.",
        issue_comments=[{"body": "Public comment"}],
        base_commit="a" * 40,
        setup_commands=["python -m pip install -e ."],
        test_commands=["pytest"],
        difficulty="medium",
        tags=["reporting", "safety"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add_all(
        [
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=["src/GOLD_FILE_MUST_NOT_APPEAR.py"],
                patch_text="GOLD_PATCH_MUST_NOT_APPEAR",
                test_files=["tests/GOLD_TEST_MUST_NOT_APPEAR.py"],
            ),
            HiddenEvalTest(
                benchmark_task_id=task.id,
                name="HIDDEN_TEST_NAME_MUST_NOT_APPEAR",
                commands=["python HIDDEN_COMMAND_MUST_NOT_APPEAR.py"],
                files_payload={"hidden.py": "HIDDEN_PAYLOAD_MUST_NOT_APPEAR"},
                patch_text="HIDDEN_PATCH_MUST_NOT_APPEAR",
                enabled=True,
            ),
        ]
    )

    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="openai",
        model_name="report-model",
        status="failed",
        repair_attempts_used=1,
        final_patch_passed_tests=False,
        started_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
    )
    db.add(run)
    db.flush()
    config = {
        "model_provider": "openai",
        "model_name": "report-model",
        "max_steps": 8,
        "max_tool_errors": 3,
        "command_timeout_seconds": 90,
        "include_issue_comments": True,
        "enable_test_tool": True,
        "run_mode": "tool_loop",
        "max_repair_attempts": 1,
        "run_tests_after_patch": True,
        "stop_on_first_passing_patch": True,
        "include_test_failure_feedback": True,
        "run_hidden_tests": True,
    }
    db.add_all(
        [
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_run_configured",
                payload_json={"config": config},
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_tool_call",
                payload_json={
                    "tool_name": "read_file",
                    "success": True,
                    "files_read": ["src/report.py", ".benchmark/gold.py"],
                    "api_key": "sk-123456789secret",
                },
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="model_response",
                payload_json={
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "estimated_cost": 0.002,
                },
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="test_phase_completed",
                payload_json={
                    "phase": "hidden_eval",
                    "passed": False,
                    "command": "HIDDEN_EVENT_COMMAND_MUST_NOT_APPEAR",
                },
            ),
        ]
    )

    patch_text = (
        "diff --git a/src/report.py b/src/report.py\n"
        "--- a/src/report.py\n"
        "+++ b/src/report.py\n" + ("+safe report line\n" * 4_000)
    )
    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text=patch_text,
        changed_files=["src/report.py", ".benchmark/gold.py"],
        version=2,
        is_selected=True,
    )
    db.add(patch)
    db.flush()
    db.add_all(
        [
            PatchQuality(
                generated_patch_id=patch.id,
                changed_file_count=1,
                added_lines=4_000,
                removed_lines=0,
                total_changed_lines=4_000,
                max_patch_files=20,
                max_patch_changed_lines=5_000,
                changed_source_files=["src/report.py"],
                changed_test_files=[],
                changed_docs_config_files=[],
                suspicious_generated_files=[],
                unrelated_files=[],
                whitespace_only=False,
                dependency_files=[],
                lockfiles=[],
                warnings=["Large but allowed patch"],
                hard_limit_violations=[],
            ),
            HumanReview(
                generated_patch_id=patch.id,
                decision=decision,
                reviewer_name="Reviewer",
                review_notes="Checked safely; api_key=sk-123456789secret",
                reviewed_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
            ),
            StoredTestResult(
                agent_run_id=run.id,
                phase="baseline",
                command="pytest",
                passed=True,
                exit_code=0,
                stdout="baseline passed",
                duration_seconds=1.0,
            ),
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="post_patch",
                command="pytest",
                passed=False,
                exit_code=1,
                stdout="x" * (MAX_LOG_BYTES + 500),
                stderr="Bearer secret-token-value",
                duration_seconds=2.0,
            ),
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="hidden_eval",
                command="HIDDEN_RESULT_COMMAND_MUST_NOT_APPEAR",
                passed=False,
                exit_code=1,
                stdout="HIDDEN_RESULT_OUTPUT_MUST_NOT_APPEAR",
                duration_seconds=3.0,
            ),
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
                tokens_used=125,
                estimated_cost=0.002,
                execution_time_seconds=120.0,
            ),
            AgentRunFailure(
                agent_run_id=run.id,
                category="post_patch_tests_failed",
                human_readable_summary=("Post-patch tests failed; access_token=secret-token-value"),
            ),
        ]
    )
    db.commit()
    return run


def test_json_report_contains_safe_complete_run_summary(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        run_id = create_report_run(db).id

    response = client.get(f"/agent-runs/{run_id}/report.json")

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('report.json"')
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["run"]["id"] == str(run_id)
    assert payload["benchmark_task"]["issue_title"] == "Fix report generation"
    assert payload["repository"]["name"] == "reporting"
    assert payload["run_configuration"]["max_repair_attempts"] == 1
    assert payload["files_inspected"] == ["src/report.py"]
    assert payload["files_modified"] == ["src/report.py"]
    assert payload["patch_quality"]["changed_file_count"] == 1
    assert payload["evaluation_metrics"]["regression_detected"] is True
    assert payload["failure"]["category"] == "post_patch_tests_failed"
    assert payload["human_review"]["status"] == "approved"
    assert payload["usage"] == {
        "tokens_used": 125,
        "estimated_cost": 0.002,
        "execution_time_seconds": 120.0,
    }

    phases = {phase["phase"]: phase for phase in payload["test_results"]}
    assert phases["post_patch"]["results"][0]["stdout"]["truncated"] is True
    assert phases["hidden_eval"]["details_included"] is False
    assert phases["hidden_eval"]["results"] == []
    assert payload["hidden_evaluation"] == {
        "available": True,
        "passed": False,
        "run_count": 1,
        "failed_count": 1,
    }
    assert payload["final_patch"]["diff"]["truncated"] is True
    assert payload["final_patch"]["diff"]["original_size_bytes"] > MAX_DIFF_BYTES
    assert payload["final_patch"]["reference"] == f"/agent-runs/{run_id}/patch"

    serialized = json.dumps(payload)
    for forbidden in (
        "GOLD_PATCH_MUST_NOT_APPEAR",
        "GOLD_FILE_MUST_NOT_APPEAR",
        "GOLD_TEST_MUST_NOT_APPEAR",
        "HIDDEN_TEST_NAME_MUST_NOT_APPEAR",
        "HIDDEN_COMMAND_MUST_NOT_APPEAR",
        "HIDDEN_PAYLOAD_MUST_NOT_APPEAR",
        "HIDDEN_PATCH_MUST_NOT_APPEAR",
        "HIDDEN_EVENT_COMMAND_MUST_NOT_APPEAR",
        "HIDDEN_RESULT_COMMAND_MUST_NOT_APPEAR",
        "HIDDEN_RESULT_OUTPUT_MUST_NOT_APPEAR",
        "sk-123456789secret",
        "secret-token-value",
    ):
        assert forbidden not in serialized


def test_markdown_report_has_expected_sections_and_truncation_notes(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        run_id = create_report_run(db).id

    response = client.get(f"/agent-runs/{run_id}/report.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].endswith('report.md"')
    for heading in (
        "# Agent Run Evaluation Report",
        "## Run Metadata",
        "## Benchmark Task",
        "## Trace Summary",
        "## Patch Quality",
        "## Test Results",
        "## Hidden Evaluation Summary",
        "## Evaluation Metrics",
        "## Failure Classification",
        "## Human Review",
        "## Final Patch",
    ):
        assert heading in response.text
    assert "stdout truncated" in response.text
    assert "Patch diff truncated" in response.text
    assert "Command and log details withheld for hidden evaluation" in response.text
    assert "GOLD_PATCH_MUST_NOT_APPEAR" not in response.text
    assert "HIDDEN_RESULT_COMMAND_MUST_NOT_APPEAR" not in response.text
    assert "secret-token-value" not in response.text


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_report_includes_human_review_decision(
    client: TestClient,
    decision: str,
) -> None:
    with TestingSessionLocal() as db:
        run_id = create_report_run(db, decision=decision).id

    payload = client.get(f"/agent-runs/{run_id}/report.json").json()

    assert payload["human_review"]["status"] == decision
    assert payload["final_patch"]["review_status"] == decision


@pytest.mark.parametrize("extension", ["json", "md"])
def test_report_missing_run_returns_404(client: TestClient, extension: str) -> None:
    response = client.get(f"/agent-runs/11111111-1111-4111-8111-111111111111/report.{extension}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Agent run not found."
