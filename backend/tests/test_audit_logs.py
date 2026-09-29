import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit_logs import calculate_audit_event_hash
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
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
    Repository,
)
from app.models import TestResult as StoredTestResult

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app = create_app()

    def override_get_db() -> Generator[Session, None, None]:
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client


def _seed_audited_run(
    db: Session,
    *,
    secret_text: str = "",
    decision: str = "approved",
) -> tuple[AgentRun, GeneratedPatch, UUID]:
    started = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    repository = Repository(
        owner="example",
        name="audit-demo",
        url="https://github.com/example/audit-demo",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=63,
        issue_title="Add verifiable audit exports",
        issue_body="Public issue context",
        base_commit="a" * 40,
        setup_commands=[],
        test_commands=["pytest -q"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add_all(
        [
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=["GOLD_FILE_MUST_NOT_APPEAR.py"],
                patch_text="GOLD_PATCH_MUST_NOT_APPEAR",
                test_files=["tests/GOLD_TEST_MUST_NOT_APPEAR.py"],
            ),
            HiddenEvalTest(
                benchmark_task_id=task.id,
                name="HIDDEN_TEST_NAME_MUST_NOT_APPEAR",
                commands=["pytest HIDDEN_COMMAND_MUST_NOT_APPEAR.py"],
                patch_text="HIDDEN_PATCH_MUST_NOT_APPEAR",
                enabled=True,
            ),
        ]
    )
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="audit-model",
        status="failed",
        repair_attempts_used=1,
        final_patch_passed_tests=False,
        started_at=started,
        completed_at=started + timedelta(minutes=2),
    )
    db.add(run)
    db.flush()

    configured = AgentEvent(
        agent_run_id=run.id,
        event_type="agent_run_configured",
        created_at=started + timedelta(seconds=1),
        payload_json={
            "config": {
                "model_provider": "mock",
                "model_name": "audit-model",
                "max_steps": 8,
                "api_key": secret_text,
            }
        },
    )
    tool_event = AgentEvent(
        agent_run_id=run.id,
        event_type="tool_call_completed",
        created_at=started + timedelta(seconds=4),
        payload_json={
            "tool_name": "read_file",
            "success": True,
            "diagnostics": secret_text,
            "gold_patch": "EVENT_GOLD_MUST_NOT_APPEAR",
        },
    )
    db.add_all(
        [
            configured,
            AgentEvent(
                agent_run_id=run.id,
                event_type="plan_submitted",
                created_at=started + timedelta(seconds=2),
                payload_json={"accepted": True, "revision": 1},
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="hypothesis_submitted",
                created_at=started + timedelta(seconds=3),
                payload_json={"revision": 1, "status": "confirmed", "confidence": "high"},
            ),
            tool_event,
        ]
    )
    db.flush()

    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text="diff --git a/app.py b/app.py\n-old\n+new\n",
        changed_files=["app.py"],
        version=1,
        is_selected=True,
        created_at=started + timedelta(seconds=5),
    )
    db.add(patch)
    db.flush()
    review = HumanReview(
        generated_patch_id=patch.id,
        decision=decision,
        reviewer_name="Security reviewer",
        review_notes=f"Reviewed safely. {secret_text}",
        reviewed_at=started + timedelta(seconds=9),
    )
    db.add(review)
    db.flush()
    db.add_all(
        [
            AgentEvent(
                agent_run_id=run.id,
                event_type="patch_applied",
                created_at=started + timedelta(seconds=6),
                payload_json={"generated_patch_id": str(patch.id), "success": True},
            ),
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="post_patch",
                command="pytest -q",
                passed=False,
                exit_code=1,
                stdout=secret_text,
                stderr="AssertionError",
                duration_seconds=1.0,
                created_at=started + timedelta(seconds=7),
            ),
            StoredTestResult(
                agent_run_id=run.id,
                generated_patch_id=patch.id,
                phase="hidden_eval",
                command="pytest HIDDEN_COMMAND_MUST_NOT_APPEAR.py",
                passed=False,
                exit_code=1,
                stdout="HIDDEN_OUTPUT_MUST_NOT_APPEAR",
                duration_seconds=0.5,
                created_at=started + timedelta(seconds=8),
            ),
            AgentEvent(
                agent_run_id=run.id,
                event_type="human_review_recorded",
                created_at=started + timedelta(seconds=9),
                payload_json={
                    "generated_patch_id": str(patch.id),
                    "human_review_id": str(review.id),
                    "decision": decision,
                    "reviewer_name": "Security reviewer",
                },
            ),
            EvaluationMetric(
                agent_run_id=run.id,
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
                created_at=started + timedelta(seconds=10),
            ),
            AgentRunFailure(
                agent_run_id=run.id,
                category="post_patch_tests_failed",
                human_readable_summary=f"Visible tests failed. {secret_text}",
                created_at=started + timedelta(seconds=11),
            ),
        ]
    )
    db.commit()
    return run, patch, tool_event.id


def test_run_audit_events_are_ordered_and_hash_chain_verifies(
    client: TestClient, fake_secret_samples: dict[str, object]
) -> None:
    with TestingSessionLocal() as db:
        run, _, _ = _seed_audited_run(db, secret_text=str(fake_secret_samples["text"]))
        run_id = run.id

    response = client.get(f"/agent-runs/{run_id}/audit-log")

    assert response.status_code == 200
    audit = response.json()
    assert audit["scope_type"] == "agent_run"
    assert audit["event_count"] == len(audit["events"])
    assert response.headers["x-audit-hash"] == audit["final_audit_hash"]
    assert [event["sequence_number"] for event in audit["events"]] == list(
        range(1, len(audit["events"]) + 1)
    )
    assert [event["timestamp"] for event in audit["events"]] == sorted(
        event["timestamp"] for event in audit["events"]
    )

    previous_hash = None
    for event in audit["events"]:
        assert event["previous_event_hash"] == previous_hash
        fields = {key: value for key, value in event.items() if key != "event_hash"}
        assert calculate_audit_event_hash(fields) == event["event_hash"]
        previous_hash = event["event_hash"]
    assert audit["final_audit_hash"] == previous_hash


def test_audit_hash_chain_is_stable_and_changes_with_source_event(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        run, _, tool_event_id = _seed_audited_run(db)
        run_id = run.id

    first = client.get(f"/agent-runs/{run_id}/audit-log").json()
    second = client.get(f"/agent-runs/{run_id}/audit-log").json()

    assert first["events"] == second["events"]
    assert first["final_audit_hash"] == second["final_audit_hash"]

    with TestingSessionLocal() as db:
        event = db.get(AgentEvent, tool_event_id)
        event.payload_json = {**event.payload_json, "success": False}
        db.commit()

    changed = client.get(f"/agent-runs/{run_id}/audit-log").json()
    assert changed["final_audit_hash"] != first["final_audit_hash"]


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_patch_audit_includes_human_review_decision(
    client: TestClient,
    decision: str,
) -> None:
    with TestingSessionLocal() as db:
        _, patch, _ = _seed_audited_run(db, decision=decision)
        patch_id = patch.id

    response = client.get(f"/patches/{patch_id}/audit-log")

    assert response.status_code == 200
    audit = response.json()
    assert audit["scope_type"] == "generated_patch"
    assert audit["scope_id"] == str(patch_id)
    review_events = [
        event for event in audit["events"] if event["event_type"] == "human_review_recorded"
    ]
    assert len(review_events) == 1
    assert review_events[0]["actor_type"] == "human"
    assert decision in review_events[0]["sanitized_summary"]


def test_audit_excludes_protected_data_and_redacts_secrets(
    client: TestClient, fake_secret_samples: dict[str, object]
) -> None:
    with TestingSessionLocal() as db:
        run, patch, _ = _seed_audited_run(db, secret_text=str(fake_secret_samples["text"]))
        run_id, patch_id = run.id, patch.id

    run_export = client.get(f"/agent-runs/{run_id}/audit-log").json()
    patch_export = client.get(f"/patches/{patch_id}/audit-log").json()
    serialized = json.dumps([run_export, patch_export])

    for secret in fake_secret_samples["secrets"]:
        assert secret not in serialized
    for protected in (
        "GOLD_FILE_MUST_NOT_APPEAR",
        "GOLD_PATCH_MUST_NOT_APPEAR",
        "GOLD_TEST_MUST_NOT_APPEAR",
        "HIDDEN_TEST_NAME_MUST_NOT_APPEAR",
        "HIDDEN_COMMAND_MUST_NOT_APPEAR",
        "HIDDEN_PATCH_MUST_NOT_APPEAR",
        "HIDDEN_OUTPUT_MUST_NOT_APPEAR",
        "EVENT_GOLD_MUST_NOT_APPEAR",
    ):
        assert protected not in serialized
    assert "[REDACTED]" in serialized


def test_missing_run_and_patch_audit_logs_return_404(client: TestClient) -> None:
    missing = "11111111-1111-4111-8111-111111111111"

    run_response = client.get(f"/agent-runs/{missing}/audit-log")
    patch_response = client.get(f"/patches/{missing}/audit-log")

    assert run_response.status_code == 404
    assert run_response.json()["detail"] == "Agent run not found."
    assert patch_response.status_code == 404
    assert patch_response.json()["detail"] == "Generated patch not found."
