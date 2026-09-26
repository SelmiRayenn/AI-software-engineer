import json
from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import AgentEvent, AgentRun, BenchmarkTask, Repository
from app.models import TestResult as StoredTestResult
from app.test_failure_analysis import TestFailureAnalysisService as AnalysisService

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture()
def run_id(db: Session) -> UUID:
    repository = Repository(
        owner="example",
        name="failure-analysis",
        url="https://github.com/example/failure-analysis",
    )
    db.add(repository)
    db.flush()
    task = BenchmarkTask(
        repository_id=repository.id,
        issue_title="Analyze failures",
        issue_body="Expose safe test diagnostics.",
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=["pytest -q"],
        status="ready",
    )
    db.add(task)
    db.flush()
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="mock",
        status="failed",
    )
    db.add(run)
    db.commit()
    return run.id


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def test_pytest_failure_parsing_and_persistence(db: Session, run_id: UUID) -> None:
    result = _failure(
        db,
        run_id,
        stderr=(
            "________________ test_addition __________________\n"
            "tests/test_calculator.py:12: in test_addition\n"
            "    assert add(2, 3) == 5\n"
            "E   AssertionError: expected 5, actual 4\n"
            "FAILED tests/test_calculator.py::test_addition - AssertionError: expected 5\n"
        ),
    )

    analysis = AnalysisService(db, run_id).analyze_results([result])[0]

    assert analysis.likely_failing_test_names == [
        "tests/test_calculator.py::test_addition",
        "test_addition",
    ]
    assert "AssertionError" in analysis.concise_failure_summary
    assert any("tests/test_calculator.py:12" in item for item in analysis.stack_trace_snippets)
    assert "tests/test_calculator.py" in analysis.affected_file_paths
    assert analysis.event_id is not None
    events = list(
        db.scalars(select(AgentEvent).where(AgentEvent.event_type == "test_failure_analysis"))
    )
    assert len(events) == 1

    repeated = AnalysisService(db, run_id).analyze_results([result])[0]
    assert repeated.event_id == analysis.event_id
    assert (
        db.scalar(select(AgentEvent).where(AgentEvent.event_type == "test_failure_analysis"))
        is not None
    )


def test_generic_assertion_and_secret_redaction(db: Session, run_id: UUID) -> None:
    result = _failure(
        db,
        run_id,
        command="python check.py --api_key=command-secret",
        stdout="AssertionError: expected enabled, got disabled api_key=output-secret",
    )

    analysis = AnalysisService(db, run_id).analyze_results([result])[0]
    serialized = analysis.model_dump_json()

    assert "AssertionError" in analysis.concise_failure_summary
    assert analysis.command_failed is True
    assert "command-secret" not in serialized
    assert "output-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_timeout_detection(db: Session, run_id: UUID) -> None:
    result = _failure(
        db,
        run_id,
        exit_code=124,
        stderr="Command exceeded timeout of 10 seconds.",
    )

    analysis = AnalysisService(db, run_id).analyze_results([result])[0]

    assert analysis.timed_out is True
    assert analysis.command_failed is False
    assert analysis.concise_failure_summary == "Test command timed out before completing."


def test_hidden_evaluation_is_aggregate_only(db: Session, run_id: UUID) -> None:
    _failure(
        db,
        run_id,
        phase="hidden_eval",
        command="pytest hidden_tests/test_secret_case.py",
        stderr="AssertionError: HIDDEN_EXPECTATION api_key=hidden-secret",
    )
    _failure(
        db,
        run_id,
        phase="hidden_eval",
        command="pytest hidden_tests/test_other_case.py",
        stderr="[truncated to last 20 bytes] HIDDEN_STACK",
    )
    db.add(
        StoredTestResult(
            agent_run_id=run_id,
            phase="hidden_eval",
            command="pytest hidden_tests/test_passing_case.py",
            passed=True,
            exit_code=0,
            stdout="passed",
            stderr="",
        )
    )
    db.commit()

    analyses = AnalysisService(db, run_id).list_for_run()

    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis.hidden_details_redacted is True
    assert analysis.failed_result_count == 2 and analysis.total_result_count == 3
    assert analysis.failed_command is None and analysis.exit_code is None
    assert analysis.output_truncated is True
    assert analysis.likely_failing_test_names == []
    assert analysis.assertion_error_excerpts == []
    serialized = json.dumps(analysis.model_dump(mode="json"))
    assert "HIDDEN_EXPECTATION" not in serialized
    assert "hidden-secret" not in serialized
    event = db.scalar(select(AgentEvent).where(AgentEvent.event_type == "test_failure_analysis"))
    assert "HIDDEN_STACK" not in json.dumps(event.payload_json)


def test_endpoint_returns_safe_analyses(client: TestClient, db: Session, run_id: UUID) -> None:
    _failure(
        db,
        run_id,
        stderr="tests/test_api.py:9: AssertionError: api_key=endpoint-secret",
    )

    response = client.get(f"/agent-runs/{run_id}/test-failure-analysis")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["phase"] == "post_patch"
    assert payload[0]["affected_file_paths"] == ["tests/test_api.py"]
    assert "endpoint-secret" not in response.text


def test_endpoint_missing_run_returns_404(client: TestClient) -> None:
    response = client.get("/agent-runs/00000000-0000-0000-0000-000000000001/test-failure-analysis")
    assert response.status_code == 404


def _failure(
    db: Session,
    run_id: UUID,
    *,
    phase: str = "post_patch",
    command: str = "pytest -q",
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "",
) -> StoredTestResult:
    result = StoredTestResult(
        agent_run_id=run_id,
        phase=phase,
        command=command,
        passed=False,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.2,
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result
