import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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


def test_test_execution_api_records_and_lists_results(
    client: TestClient,
    workspace: Path,
) -> None:
    setup_command = python_command("print('setup api')")
    test_command = python_command("print('baseline api')")
    run_id = create_agent_run(
        workspace,
        setup_commands=[setup_command],
        test_commands=[test_command],
    )

    baseline_response = client.post(f"/agent-runs/{run_id}/tests/baseline", json={})

    assert baseline_response.status_code == 200
    baseline_payload = baseline_response.json()
    assert baseline_payload["passed"] is True
    assert baseline_payload["setup_results"][0]["phase"] == "setup"
    assert baseline_payload["test_results"][0]["phase"] == "baseline"

    list_response = client.get(f"/agent-runs/{run_id}/tests")

    assert list_response.status_code == 200
    listed = list_response.json()
    assert sorted(result["phase"] for result in listed) == ["baseline", "setup"]


def test_post_patch_api_applies_generated_patch_and_records_results(
    client: TestClient,
    workspace: Path,
) -> None:
    test_command = python_command(
        "from pathlib import Path; "
        "assert 'a + b + 1' in Path('src/calculator.py').read_text(); "
        "print('patched api')"
    )
    run_id = create_agent_run(workspace, test_commands=[test_command])
    with TestingSessionLocal() as db:
        db.add(
            GeneratedPatch(
                agent_run_id=run_id,
                patch_text=calculator_patch(),
                changed_files=["src/calculator.py"],
            )
        )
        db.commit()

    response = client.post(f"/agent-runs/{run_id}/tests/post-patch", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["passed"] is True
    assert payload["patch_status"] == "applied"
    assert payload["test_results"][0]["phase"] == "post_patch"


def create_agent_run(
    workspace: Path,
    *,
    setup_commands: list[str] | None = None,
    test_commands: list[str] | None = None,
) -> UUID:
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
            setup_commands=setup_commands or [],
            test_commands=test_commands or [python_command("print('ok')")],
            status="ready",
        )
        db.add(task)
        db.flush()

        run = AgentRun(
            benchmark_task_id=task.id,
            model_provider="mock",
            model_name="scripted-mock",
            status="queued",
            workspace_id="test-workspace",
            workspace_path=str(workspace),
        )
        db.add(run)
        db.commit()
        return run.id


def python_command(code: str) -> str:
    escaped = code.replace('"', '\\"')
    return f'"{sys.executable}" -c "{escaped}"'


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
