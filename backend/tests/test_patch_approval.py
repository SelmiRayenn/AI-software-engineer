from __future__ import annotations

from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.approvals import PatchApprovalExportBlockedError, PatchApprovalService
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentRun,
    BenchmarkTask,
    GeneratedPatch,
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


def test_approve_patch(client: TestClient, db: Session) -> None:
    patch_id = create_generated_patch(db)

    response = client.post(
        f"/patches/{patch_id}/approve",
        json={"reviewer_name": "Ada", "review_notes": "Looks correct."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "approved"
    assert payload["export_eligible"] is True
    assert payload["review"]["decision"] == "approved"
    assert payload["review"]["reviewer_name"] == "Ada"

    with TestingSessionLocal() as session:
        review = session.scalar(select(HumanReview).where(HumanReview.generated_patch_id == patch_id))
        assert review is not None
        assert review.decision == "approved"


def test_reject_patch(client: TestClient, db: Session) -> None:
    patch_id = create_generated_patch(db)

    response = client.post(
        f"/patches/{patch_id}/reject",
        json={"reviewer_name": "Grace", "review_notes": "Patch changes the wrong file."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_status"] == "rejected"
    assert payload["export_eligible"] is False
    assert payload["review"]["decision"] == "rejected"
    assert payload["review"]["review_notes"] == "Patch changes the wrong file."


def test_rejection_requires_notes(client: TestClient, db: Session) -> None:
    patch_id = create_generated_patch(db)

    response = client.post(
        f"/patches/{patch_id}/reject",
        json={"reviewer_name": "Grace"},
    )

    assert response.status_code == 422


def test_duplicate_review_requires_explicit_update(client: TestClient, db: Session) -> None:
    patch_id = create_generated_patch(db)
    first = client.post(
        f"/patches/{patch_id}/approve",
        json={"reviewer_name": "Ada"},
    )

    duplicate = client.post(
        f"/patches/{patch_id}/reject",
        json={"reviewer_name": "Grace", "review_notes": "Changing decision."},
    )
    updated = client.post(
        f"/patches/{patch_id}/reject",
        json={
            "reviewer_name": "Grace",
            "review_notes": "Changing decision.",
            "update_existing": True,
        },
    )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["review_status"] == "rejected"
    with TestingSessionLocal() as session:
        assert session.scalar(select(HumanReview).where(HumanReview.generated_patch_id == patch_id))
        assert len(list(session.scalars(select(HumanReview)).all())) == 1


def test_approved_status_appears_in_patch_and_run_responses(
    client: TestClient,
    db: Session,
) -> None:
    patch_id = create_generated_patch(db)
    response = client.post(
        f"/patches/{patch_id}/approve",
        json={"reviewer_name": "Ada"},
    )
    assert response.status_code == 200

    with TestingSessionLocal() as session:
        generated_patch = session.get(GeneratedPatch, patch_id)
        run_id = generated_patch.agent_run_id

    patch_response = client.get(f"/agent-runs/{run_id}/patch")
    runs_response = client.get("/api/v1/agent-runs")

    assert patch_response.status_code == 200
    assert patch_response.json()["review_status"] == "approved"
    assert runs_response.status_code == 200
    run_payload = next(run for run in runs_response.json() if run["id"] == str(run_id))
    assert run_payload["patch_review_status"] == "approved"


def test_rejected_patch_is_blocked_from_export_placeholder(db: Session) -> None:
    patch_id = create_generated_patch(db)
    service = PatchApprovalService(db=db)
    service.reject_patch(
        patch_id,
        reviewer_name="Grace",
        review_notes="Patch should not be exported.",
    )

    with pytest.raises(PatchApprovalExportBlockedError):
        service.ensure_patch_exportable(patch_id)


def test_approved_patch_is_exportable(db: Session) -> None:
    patch_id = create_generated_patch(db)
    service = PatchApprovalService(db=db)
    service.approve_patch(patch_id, reviewer_name="Ada")

    generated_patch = service.ensure_patch_exportable(patch_id)

    assert generated_patch.id == patch_id


def test_missing_patch_returns_clear_404(client: TestClient) -> None:
    missing_patch_id = uuid4()

    response = client.get(f"/patches/{missing_patch_id}/review")

    assert response.status_code == 404
    assert response.json()["detail"] == "Generated patch not found."


def test_agent_actor_cannot_approve_patch(client: TestClient, db: Session) -> None:
    patch_id = create_generated_patch(db)

    response = client.post(
        f"/patches/{patch_id}/approve",
        headers={"X-Actor-Type": "agent"},
        json={"reviewer_name": "scripted-agent"},
    )

    assert response.status_code == 403


def create_generated_patch(db: Session) -> UUID:
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

    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="scripted-mock",
        status="completed",
    )
    db.add(run)
    db.flush()

    generated_patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text="diff --git a/src/calculator.py b/src/calculator.py\n",
        changed_files=["src/calculator.py"],
    )
    db.add(generated_patch)
    db.commit()
    return generated_patch.id
