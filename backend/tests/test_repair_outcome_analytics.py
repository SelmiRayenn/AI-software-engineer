from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

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
    BenchmarkPack,
    BenchmarkPackRun,
    BenchmarkPackRunTask,
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    Repository,
)
from app.models import TestResult as StoredTestResult

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


def test_empty_repair_outcomes_have_defined_zero_state(client: TestClient) -> None:
    response = client.get("/analytics/repair-outcomes")

    assert response.status_code == 200
    assert response.json() == {
        "total_runs_with_repairs": 0,
        "average_repair_attempts": 0.0,
        "repair_success_rate": 0.0,
        "first_patch_pass_rate": 0.0,
        "repaired_patch_pass_rate": 0.0,
        "attempts_exhausted_rate": 0.0,
        "most_common_initial_failure_categories": [],
        "most_common_repair_failure_categories": [],
        "average_cost_with_repairs": 0.0,
        "average_time_with_repairs": 0.0,
        "repair_success_by_model": [],
        "repair_success_by_repository": [],
    }


def test_successful_repair_and_first_patch_success_are_counted(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "calculator")
        repaired = create_run(
            db,
            task,
            provider="openai",
            model="repair-model",
            repair_attempts=1,
            final_passed=True,
            cost=0.4,
            duration=40.0,
        )
        add_attempt(db, repaired, 1, "failed_tests", passed=False)
        add_attempt(db, repaired, 2, "passed", passed=True)
        add_patch_versions(db, repaired, 2)

        first_pass = create_run(
            db,
            task,
            provider="openai",
            model="repair-model",
            repair_attempts=0,
            final_passed=True,
            cost=0.1,
            duration=10.0,
        )
        add_attempt(db, first_pass, 1, "passed", passed=True)
        add_patch_versions(db, first_pass, 1)
        db.commit()

    payload = client.get("/analytics/repair-outcomes").json()

    assert payload["total_runs_with_repairs"] == 1
    assert payload["average_repair_attempts"] == 1.0
    assert payload["repair_success_rate"] == 1.0
    assert payload["first_patch_pass_rate"] == 0.5
    assert payload["repaired_patch_pass_rate"] == 1.0
    assert payload["attempts_exhausted_rate"] == 0.0
    assert payload["average_cost_with_repairs"] == 0.4
    assert payload["average_time_with_repairs"] == 40.0
    assert payload["most_common_initial_failure_categories"] == [
        {"category": "post_patch_tests_failed", "count": 1}
    ]
    assert payload["repair_success_by_model"][0]["repair_success_rate"] == 1.0
    assert payload["repair_success_by_repository"][0]["repository_name"] == "calculator"


def test_exhausted_repair_is_counted_with_failure_categories(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "exhausted")
        run = create_run(
            db,
            task,
            repair_attempts=2,
            final_passed=False,
            status="failed",
            cost=0.3,
            duration=90.0,
        )
        for attempt in range(1, 4):
            add_attempt(db, run, attempt, "failed_tests", passed=False)
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="repair_limit_reached",
                payload_json={"max_repair_attempts": 2},
            )
        )
        db.add(
            AgentRunFailure(
                agent_run_id=run.id,
                category="max_repair_attempts_reached",
                human_readable_summary="Repair attempts were exhausted.",
            )
        )
        db.commit()

    payload = client.get("/analytics/repair-outcomes").json()

    assert payload["total_runs_with_repairs"] == 1
    assert payload["average_repair_attempts"] == 2.0
    assert payload["repair_success_rate"] == 0.0
    assert payload["repaired_patch_pass_rate"] == 0.0
    assert payload["attempts_exhausted_rate"] == 1.0
    assert payload["most_common_repair_failure_categories"] == [
        {"category": "post_patch_tests_failed", "count": 2},
        {"category": "max_repair_attempts_reached", "count": 1},
    ]


def test_legacy_patch_versions_and_numbered_tests_provide_fallback(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "legacy", "repair-history")
        run = create_run(
            db,
            task,
            repair_attempts=0,
            final_passed=True,
        )
        add_patch_versions(db, run, 2)
        for attempt, passed in ((1, False), (2, True)):
            db.add(
                StoredTestResult(
                    agent_run_id=run.id,
                    phase="post_patch",
                    attempt_number=attempt,
                    command="pytest",
                    passed=passed,
                    exit_code=0 if passed else 1,
                )
            )
        db.commit()

    payload = client.get("/analytics/repair-outcomes").json()

    assert payload["total_runs_with_repairs"] == 1
    assert payload["average_repair_attempts"] == 1.0
    assert payload["repair_success_rate"] == 1.0
    assert payload["repaired_patch_pass_rate"] == 1.0


def test_repair_outcome_filters_select_only_matching_runs(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        included_task = create_task(db, "acme", "included")
        excluded_task = create_task(db, "other", "excluded")
        included = create_run(
            db,
            included_task,
            provider="mock",
            model="included-model",
            repair_attempts=1,
            final_passed=True,
            started_at=datetime(2026, 2, 10, tzinfo=UTC),
        )
        excluded = create_run(
            db,
            excluded_task,
            provider="local",
            model="excluded-model",
            repair_attempts=1,
            final_passed=False,
            started_at=datetime(2026, 3, 10, tzinfo=UTC),
        )
        for run, final_outcome in ((included, "passed"), (excluded, "failed_tests")):
            add_attempt(db, run, 1, "failed_tests", passed=False)
            add_attempt(db, run, 2, final_outcome, passed=final_outcome == "passed")
        pack = attach_to_pack(db, included)
        db.commit()
        filters = {
            "benchmark_pack_id": str(pack.id),
            "repository_id": str(included_task.repository_id),
            "model_provider": "mock",
            "model_name": "included-model",
            "date_from": "2026-02-01T00:00:00Z",
            "date_to": "2026-02-28T23:59:59Z",
        }

    payload = client.get("/analytics/repair-outcomes", params=filters).json()

    assert payload["total_runs_with_repairs"] == 1
    assert payload["repair_success_rate"] == 1.0
    assert payload["repair_success_by_model"][0]["model_name"] == "included-model"
    assert (
        client.get("/analytics/repair-outcomes", params={"model_provider": "anthropic"}).json()[
            "total_runs_with_repairs"
        ]
        == 0
    )


def create_task(db: Session, owner: str, name: str) -> BenchmarkTask:
    repository = Repository(
        owner=owner,
        name=name,
        url=f"https://github.com/{owner}/{name}",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_title=f"Fix {name}",
        issue_body="A reproducible issue",
        base_commit="a" * 40,
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    return task


def create_run(
    db: Session,
    task: BenchmarkTask,
    *,
    repair_attempts: int,
    final_passed: bool,
    provider: str = "mock",
    model: str = "mock-model",
    status: str = "completed",
    cost: float = 0.0,
    duration: float = 30.0,
    started_at: datetime | None = None,
) -> AgentRun:
    started = started_at or datetime(2026, 1, 15, tzinfo=UTC)
    run = AgentRun(
        benchmark_task=task,
        model_provider=provider,
        model_name=model,
        status=status,
        repair_attempts_used=repair_attempts,
        final_patch_passed_tests=final_passed,
        started_at=started,
        completed_at=started + timedelta(seconds=duration),
    )
    db.add(run)
    db.flush()
    db.add(
        EvaluationMetric(
            agent_run_id=run.id,
            patch_applied=True,
            tests_passed=final_passed,
            post_patch_tests_passed=final_passed,
            issue_resolved=final_passed,
            estimated_cost=cost,
            execution_time_seconds=duration,
        )
    )
    return run


def add_attempt(
    db: Session,
    run: AgentRun,
    attempt: int,
    outcome: str,
    *,
    passed: bool,
) -> None:
    db.add(
        AgentEvent(
            agent_run_id=run.id,
            event_type="repair_attempt_completed",
            payload_json={
                "attempt_number": attempt,
                "repair_attempts_used": max(0, attempt - 1),
                "outcome": outcome,
                "tests_passed": passed,
            },
            created_at=run.started_at + timedelta(seconds=attempt),
        )
    )
    db.add(
        StoredTestResult(
            agent_run_id=run.id,
            phase="post_patch",
            attempt_number=attempt,
            command="pytest",
            passed=passed,
            exit_code=0 if passed else 1,
            duration_seconds=1.0,
            created_at=run.started_at + timedelta(seconds=attempt),
        )
    )


def add_patch_versions(db: Session, run: AgentRun, count: int) -> None:
    for version in range(1, count + 1):
        db.add(
            GeneratedPatch(
                agent_run_id=run.id,
                patch_text=f"patch version {version}",
                changed_files=["app.py"],
                version=version,
                is_selected=version == count,
            )
        )


def attach_to_pack(db: Session, run: AgentRun) -> BenchmarkPack:
    pack = BenchmarkPack(name="Repair Pack", slug="repair-pack", version="1")
    db.add(pack)
    db.flush()
    pack_run = BenchmarkPackRun(
        benchmark_pack_id=pack.id,
        pack_name=pack.name,
        pack_slug=pack.slug,
        pack_version=pack.version,
        model_provider=run.model_provider,
        model_name=run.model_name,
        run_config={},
        status="completed",
        started_at=run.started_at,
        completed_at=run.completed_at,
        aggregates={},
    )
    db.add(pack_run)
    db.flush()
    db.add(
        BenchmarkPackRunTask(
            benchmark_pack_run_id=pack_run.id,
            benchmark_task_id=run.benchmark_task_id,
            agent_run_id=run.id,
            order_index=0,
            task_snapshot={},
            definition_hash="a" * 64,
            status=run.status,
            metric_summary={},
        )
    )
    return pack
