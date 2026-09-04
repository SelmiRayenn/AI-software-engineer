import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import AgentRun, BenchmarkTask, GeneratedPatch, Repository

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
def client() -> Generator[TestClient, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    init_git_repo(root)
    return root


def test_agent_run_patch_api_round_trip(client: TestClient, workspace: Path) -> None:
    run_id = create_agent_run(workspace)

    diff_response = client.get(f"/agent-runs/{run_id}/diff")

    assert diff_response.status_code == 200
    assert diff_response.json()["changed_files"] == []

    apply_response = client.post(
        f"/agent-runs/{run_id}/patch/apply",
        json={"patch_text": calculator_patch()},
    )

    assert apply_response.status_code == 200
    apply_payload = apply_response.json()
    assert apply_payload["changed_files"] == ["src/calculator.py"]
    generated_patch_id = apply_payload["generated_patch_id"]

    patch_response = client.get(f"/agent-runs/{run_id}/patch")

    assert patch_response.status_code == 200
    patch_payload = patch_response.json()
    assert patch_payload["id"] == generated_patch_id
    assert patch_payload["patch_text"] == calculator_patch()
    assert patch_payload["stats"]["changed_files_count"] == 1

    with TestingSessionLocal() as db:
        stored_patch = db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == run_id))
        assert stored_patch is not None


def test_patch_apply_endpoint_rejects_completed_run(
    client: TestClient,
    workspace: Path,
) -> None:
    run_id = create_agent_run(workspace, status="completed")

    response = client.post(
        f"/agent-runs/{run_id}/patch/apply",
        json={"patch_text": calculator_patch()},
    )

    assert response.status_code == 422
    assert "only allowed" in response.json()["detail"]


def create_agent_run(workspace: Path, *, status: str = "running") -> UUID:
    with TestingSessionLocal() as db:
        repository = Repository(
            name="calculator",
            owner="example",
            url="https://github.com/example/calculator",
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
            status=status,
            workspace_id="test-workspace",
            workspace_path=str(workspace),
        )
        db.add(run)
        db.commit()
        return run.id


def calculator_patch() -> str:
    return (
        "diff --git a/src/calculator.py b/src/calculator.py\n"
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b + 1\n"
    )


def init_git_repo(workspace: Path) -> None:
    run_git(workspace, "init")
    run_git(workspace, "config", "user.email", "tester@example.com")
    run_git(workspace, "config", "user.name", "Test User")
    run_git(workspace, "add", ".")
    run_git(workspace, "commit", "-m", "initial")


def run_git(workspace: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
