from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.failures.categories import FAILURE_CATEGORIES
from app.failures.service import (
    AgentRunNotFoundError,
    FailureClassificationService,
)
from app.models import AgentEvent, AgentRun, AgentRunFailure, BenchmarkTask, Repository

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_all_supported_failure_categories_are_registered() -> None:
    assert FAILURE_CATEGORIES == {
        "task_not_ready",
        "repository_checkout_failed",
        "docker_unavailable",
        "setup_failed",
        "baseline_tests_failed",
        "model_provider_error",
        "malformed_tool_call",
        "unknown_tool",
        "tool_error_limit_reached",
        "patch_generation_failed",
        "patch_apply_failed",
        "patch_quality_blocked",
        "post_patch_tests_failed",
        "max_steps_reached",
        "max_repair_attempts_reached",
        "timeout",
        "cancelled",
        "unknown",
    }


def test_classification_is_stored_redacted_and_updated_in_place(db: Session) -> None:
    run_id = create_run(db)
    event = AgentEvent(
        agent_run_id=run_id,
        event_type="agent_run_failed",
        payload_json={},
    )
    db.add(event)
    db.commit()
    service = FailureClassificationService(db)

    first = service.classify(
        agent_run_id=run_id,
        category="model_provider_error",
        summary="Provider failed; api_key=private-value",
        source_event_id=event.id,
    )
    first_summary = first.human_readable_summary
    second = service.classify(
        agent_run_id=run_id,
        category="timeout",
        summary="Command timed out.",
    )

    assert first.id == second.id
    assert second.category == "timeout"
    assert second.human_readable_summary == "Command timed out."
    assert db.scalar(select(func.count()).select_from(AgentRunFailure)) == 1
    assert "private-value" not in first_summary
    assert "[REDACTED]" in first_summary


def test_legacy_failed_run_is_classified_from_source_event(db: Session) -> None:
    run_id = create_run(db)
    event = AgentEvent(
        agent_run_id=run_id,
        event_type="test_phase_completed",
        payload_json={"phase": "baseline", "passed": False},
    )
    db.add(event)
    db.commit()

    failure = FailureClassificationService(db).get_or_classify(run_id)

    assert failure.category == "baseline_tests_failed"
    assert failure.source_event_id == event.id


def test_cancelled_run_is_classified_lazily(db: Session) -> None:
    run_id = create_run(db, status="cancelled")

    failure = FailureClassificationService(db).get_or_classify(run_id)

    assert failure.category == "cancelled"


def test_missing_run_returns_clear_error(db: Session) -> None:
    with pytest.raises(AgentRunNotFoundError, match="Agent run not found"):
        FailureClassificationService(db).get_or_classify(uuid4())


def create_run(db: Session, *, status: str = "failed") -> UUID:
    repository = Repository(
        name="calculator",
        owner="example",
        url="https://github.com/example/calculator",
        default_branch="main",
    )
    db.add(repository)
    db.flush()
    task = BenchmarkTask(
        repository_id=repository.id,
        issue_number=1,
        issue_title="Fix calculator",
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="mock",
        status=status,
    )
    db.add(run)
    db.commit()
    return run.id
