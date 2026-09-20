from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.flakiness_checks import get_flakiness_runner
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.flakiness import FlakinessCheckService
from app.main import create_app
from app.models import BenchmarkTask, FlakinessCheck, FlakinessCheckRun, Repository
from app.schemas.flakiness import FlakinessCheckRequest
from app.schemas.sandbox import SandboxCommandResult, SandboxRunRequest, SandboxRunResponse


class FakeRunner:
    def __init__(self, response: SandboxRunResponse) -> None:
        self.response = response
        self.requests: list[SandboxRunRequest] = []

    def run(self, request: SandboxRunRequest) -> SandboxRunResponse:
        self.requests.append(request)
        return self.response


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def create_task(db: Session, *, setup: list[str] | None = None, tests: list[str] | None = None):
    repository = Repository(
        owner="example", name="flaky", url="https://github.com/example/flaky"
    )
    task = BenchmarkTask(
        repository=repository,
        issue_title="Detect flaky baseline",
        base_commit="a" * 40,
        setup_commands=setup if setup is not None else ["python -m pip install -e ."],
        test_commands=tests if tests is not None else ["pytest -q"],
    )
    db.add(task)
    db.commit()
    return task


def command_result(
    passed: bool,
    *,
    phase: str = "test",
    command: str = "pytest -q",
    duration: float = 1.0,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        phase=phase,
        command=command,
        passed=passed,
        exit_code=0 if passed else 1,
        stdout="ok" if passed else "",
        stderr="" if passed else "failed",
        duration_seconds=duration,
    )


def sandbox_response(
    outcomes: list[bool],
    *,
    status: str | None = None,
    setup_passed: bool = True,
    retained: bool = False,
) -> SandboxRunResponse:
    clone = command_result(True, phase="clone", command="git clone", duration=0.1)
    checkout = command_result(True, phase="checkout", command="git checkout", duration=0.1)
    setup = [command_result(setup_passed, phase="setup", command="python -m pip install -e .")]
    return SandboxRunResponse(
        status=status or ("passed" if all(outcomes) else "tests_failed"),
        workspace_id="workspace-1",
        workspace_path="/sandbox/workspace-1",
        workspace_root="/sandbox",
        workspace_retained=retained,
        repository_url="https://github.com/example/flaky",
        base_commit="a" * 40,
        image="python:3.12-slim",
        network_enabled=False,
        command_timeout_seconds=30,
        clone_result=clone,
        checkout_result=checkout,
        setup_results=setup,
        test_results=[command_result(outcome) for outcome in outcomes],
    )


def test_stable_task_persists_repetitions_and_metrics(db: Session) -> None:
    task = create_task(db)
    runner = FakeRunner(sandbox_response([True, True, True]))
    check = FlakinessCheckService(db, runner=runner).run(task.id, FlakinessCheckRequest())

    assert check.status == "stable"
    assert check.repetitions_requested == check.repetitions_completed == 3
    assert check.pass_count == 3 and check.fail_count == 0
    assert check.inconsistent_results is False
    assert check.average_duration_seconds == pytest.approx(1.0)
    assert len(check.runs) == 3
    assert db.scalar(select(FlakinessCheck).where(FlakinessCheck.id == check.id)) is not None
    assert len(db.scalars(select(FlakinessCheckRun)).all()) == 3


def test_alternating_results_are_flaky(db: Session) -> None:
    task = create_task(db)
    check = FlakinessCheckService(
        db, runner=FakeRunner(sandbox_response([True, False, True]))
    ).run(task.id, FlakinessCheckRequest())

    assert check.status == "flaky"
    assert check.pass_count == 2 and check.fail_count == 1
    assert check.inconsistent_results is True


def test_setup_failure_is_persisted_without_repetitions(db: Session) -> None:
    task = create_task(db)
    response = sandbox_response([], status="setup_failed", setup_passed=False)
    check = FlakinessCheckService(db, runner=FakeRunner(response)).run(
        task.id, FlakinessCheckRequest()
    )

    assert check.status == "failed_setup"
    assert check.repetitions_completed == check.pass_count == check.fail_count == 0
    assert check.error_summary == "Configured setup command failed."
    assert check.setup_results[0]["passed"] is False


def test_stop_on_first_failure_is_forwarded_and_stops_persisted_runs(db: Session) -> None:
    task = create_task(db)
    runner = FakeRunner(sandbox_response([False]))
    check = FlakinessCheckService(db, runner=runner).run(
        task.id,
        FlakinessCheckRequest(repetitions=5, stop_on_first_failure=True),
    )

    assert check.status == "inconclusive"
    assert check.repetitions_completed == 1 and check.fail_count == 1
    assert runner.requests[0].stop_on_first_test_failure is True
    assert runner.requests[0].test_commands == ["pytest -q"]
    assert runner.requests[0].test_repetitions == 5


def test_api_creates_and_lists_checks(client_factory, db: Session) -> None:
    task = create_task(db)
    runner = FakeRunner(sandbox_response([True, False, True], retained=True))
    with client_factory(runner) as client:
        created = client.post(
            f"/benchmark-tasks/{task.id}/flakiness-check",
            json={"repetitions": 3, "command_timeout_seconds": 30},
        )
        listed = client.get(f"/benchmark-tasks/{task.id}/flakiness-checks")

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "flaky"
    assert created.json()["workspace_retained"] is True
    assert len(created.json()["runs"]) == 3
    assert listed.status_code == 200 and listed.json()[0]["id"] == created.json()["id"]


@pytest.fixture()
def client_factory(db: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "database_auto_create_tables", False)

    def factory(runner: FakeRunner):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_flakiness_runner] = lambda: runner
        return TestClient(app)

    return factory


def test_command_safety_rejects_client_commands_and_missing_task_configuration(
    client_factory, db: Session
) -> None:
    task = create_task(db, setup=[], tests=[])
    runner = FakeRunner(sandbox_response([]))
    with client_factory(runner) as client:
        arbitrary = client.post(
            f"/benchmark-tasks/{task.id}/flakiness-check",
            json={"repetitions": 3, "test_commands": ["rm -rf ."]},
        )
        missing = client.post(
            f"/benchmark-tasks/{task.id}/flakiness-check", json={"repetitions": 3}
        )

    assert arbitrary.status_code == 422
    assert missing.status_code == 422
    assert runner.requests == []


def test_docker_error_is_inconclusive_and_redacted(db: Session) -> None:
    task = create_task(db)
    response = sandbox_response([], status="sandbox_error")
    response.error_code = "docker_unavailable"
    response.error = "Docker is unavailable at C:/private/socket"
    check = FlakinessCheckService(db, runner=FakeRunner(response)).run(
        task.id, FlakinessCheckRequest()
    )

    assert check.status == "inconclusive"
    assert check.error_code == "docker_unavailable"
    assert check.error_summary == "Docker is unavailable for the flakiness check."
    assert "private" not in check.error_summary
