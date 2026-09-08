import json
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.orchestrator import PreparedWorkspace, WorkspacePreparationError
from app.api.routes.agent_run_orchestration import (
    get_model_provider_factory,
    get_workspace_preparer,
)
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.model_providers import ModelProviderFactory
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    ChunkEmbedding,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    Repository,
    RepositoryIndex,
)
from app.models import TestResult as ResultRecord
from app.sandbox import SandboxWorkspaceManager

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeWorkspacePreparer:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.fail = False
        self.calls: list[dict[str, object]] = []

    def prepare(
        self,
        *,
        repository_url: str,
        base_commit: str,
        command_timeout_seconds: int,
    ) -> PreparedWorkspace:
        self.calls.append(
            {
                "repository_url": repository_url,
                "base_commit": base_commit,
                "command_timeout_seconds": command_timeout_seconds,
            }
        )
        if self.fail:
            raise WorkspacePreparationError("sandbox workspace failed")
        return PreparedWorkspace(workspace_id="test-workspace", path=self.workspace)


def override_get_db() -> Generator[Session, None, None]:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    init_git_repo(root)
    return root


@pytest.fixture()
def workspace_preparer(workspace: Path) -> FakeWorkspacePreparer:
    return FakeWorkspacePreparer(workspace)


@pytest.fixture()
def client(
    workspace_preparer: FakeWorkspacePreparer,
) -> Generator[TestClient, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_workspace_preparer] = lambda: workspace_preparer
    app.dependency_overrides[get_model_provider_factory] = lambda: ModelProviderFactory()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_starting_run_on_ready_task_completes_noop_patch(
    client: TestClient,
    workspace_preparer: FakeWorkspacePreparer,
) -> None:
    task_id = create_task(status="ready")

    response = client.post(
        f"/agent-runs/{task_id}/start",
        json={
            "model_provider": "mock",
            "model_name": "scripted-mock",
            "max_steps": 4,
            "command_timeout_seconds": 15,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["model_provider"] == "mock"
    assert payload["model_name"] == "scripted-mock"
    assert [step["step_name"] for step in payload["steps"]] == [
        "list_files",
        "read_file",
        "get_diff",
        "submit_patch",
    ]
    assert payload["changed_files"] == []
    assert payload["generated_patch_id"] is not None
    assert workspace_preparer.calls == [
        {
            "repository_url": "https://github.com/example/calculator",
            "base_commit": "1111111111111111111111111111111111111111",
            "command_timeout_seconds": 15,
        }
    ]


def test_start_rejects_draft_task(client: TestClient) -> None:
    task_id = create_task(status="draft")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 409
    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_start_creates_agent_run_row_and_logs_events(client: TestClient) -> None:
    task_id = create_task(status="ready")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        run = db.get(AgentRun, run_id)
        assert run is not None
        assert run.status == "completed"
        events = list(db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run_id)).all())

    event_types = [event.event_type for event in events]
    assert "agent_run_started" in event_types
    assert "model_response" in event_types
    assert event_types.count("agent_tool_call") == 4
    assert "agent_run_completed" in event_types


def test_start_records_setup_baseline_and_post_patch_results(client: TestClient) -> None:
    setup_command = python_command("print('setup from orchestrator')")
    test_command = python_command("print('tests from orchestrator')")
    task_id = create_task(
        status="ready",
        setup_commands=[setup_command],
        test_commands=[test_command],
    )

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        results = list(
            db.scalars(
                select(ResultRecord)
                .where(ResultRecord.agent_run_id == run_id)
                .order_by(ResultRecord.created_at.asc())
            ).all()
        )

    assert sorted(result.phase for result in results) == ["baseline", "post_patch", "setup"]
    assert all(result.passed for result in results)


@pytest.mark.parametrize("auto_embed", [False, True])
def test_managed_run_creates_repository_index_before_agent_loop(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_embed: bool,
) -> None:
    monkeypatch.setattr(settings, "embedding_auto_build", auto_embed)
    monkeypatch.setattr(settings, "embeddings_provider", "mock")
    monkeypatch.setattr(settings, "enable_real_embeddings", False)
    manager = SandboxWorkspaceManager(
        workspace_root=tmp_path / "sandboxes",
        retain_workspaces=True,
    )
    metadata = manager.create_workspace(prefix="agent-run")
    metadata.repo_path.mkdir()
    (metadata.repo_path / "README.md").write_text("# Indexed run\n", encoding="utf-8")
    (metadata.repo_path / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    init_git_repo(metadata.repo_path)
    app.dependency_overrides[get_workspace_preparer] = lambda: ManagedWorkspacePreparer(
        manager,
        metadata,
    )
    task_id = create_task(status="ready")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        index = db.scalar(select(RepositoryIndex).where(RepositoryIndex.agent_run_id == run_id))
        event = db.scalar(
            select(AgentEvent).where(
                AgentEvent.agent_run_id == run_id,
                AgentEvent.event_type == "repository_index_created",
            )
        )

        assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) == (
            2 if auto_embed else 0
        )

    assert index is not None
    assert index.file_count == 2
    assert event is not None
    assert event.payload_json["file_count"] == 2


def test_start_uses_mock_model_provider_and_hides_gold_data(client: TestClient) -> None:
    task_id = create_task(status="ready", gold_patch_text="HIDDEN GOLD PATCH")

    response = client.post(
        f"/agent-runs/{task_id}/start",
        json={"model_provider": "mock", "model_name": "mock-trace"},
    )

    assert response.status_code == 200
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        events = list(db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run_id)).all())

    model_events = [event for event in events if event.event_type == "model_response"]
    assert model_events[0].payload_json["provider_name"] == "mock"
    assert model_events[0].payload_json["model_name"] == "mock-trace"
    serialized_events = json.dumps([event.payload_json for event in events])
    assert "HIDDEN GOLD PATCH" not in serialized_events


def test_noop_run_persists_empty_generated_patch(client: TestClient) -> None:
    task_id = create_task(status="ready")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        patch = db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == run_id))
        assert patch is not None
        assert patch.patch_text == ""
        assert patch.changed_files == []


def test_completed_run_persists_evaluation_metric(client: TestClient) -> None:
    task_id = create_task(status="ready")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    run_id = UUID(response.json()["id"])
    with TestingSessionLocal() as db:
        metric = db.scalar(select(EvaluationMetric).where(EvaluationMetric.agent_run_id == run_id))
        assert metric is not None
        assert metric.tests_passed is True
        assert metric.tokens_used == 0
        assert metric.estimated_cost == 0.0


def test_start_marks_run_failed_on_workspace_error(
    client: TestClient,
    workspace_preparer: FakeWorkspacePreparer,
) -> None:
    workspace_preparer.fail = True
    task_id = create_task(status="ready")

    response = client.post(f"/agent-runs/{task_id}/start", json={"model_provider": "mock"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error_message"] == "sandbox workspace failed"

    with TestingSessionLocal() as db:
        run = db.get(AgentRun, UUID(payload["id"]))
        assert run is not None
        assert run.status == "failed"
        events = list(db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run.id)).all())

    assert events[-1].event_type == "agent_run_failed"


def test_start_stops_after_max_steps(client: TestClient) -> None:
    task_id = create_task(status="ready")

    response = client.post(
        f"/agent-runs/{task_id}/start",
        json={"model_provider": "mock", "max_steps": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert len(payload["steps"]) == 2
    assert "Maximum step limit" in payload["error_message"]


def test_invalid_run_configuration_is_rejected(client: TestClient) -> None:
    task_id = create_task(status="ready")

    response = client.post(
        f"/agent-runs/{task_id}/start",
        json={"model_provider": "mock", "run_mode": "unrestricted"},
    )

    assert response.status_code == 422
    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_run_configuration_and_prompt_preview_are_stored_and_inspectable(
    client: TestClient,
) -> None:
    task_id = create_task(status="ready")
    request_payload = {
        "model_provider": "mock",
        "model_name": "repeatable-mock",
        "max_steps": 4,
        "max_tool_errors": 2,
        "command_timeout_seconds": 15,
        "include_issue_comments": False,
        "enable_test_tool": False,
        "run_mode": "scripted",
    }

    response = client.post(f"/agent-runs/{task_id}/start", json=request_payload)

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_config"] == request_payload
    assert payload["prompt_preview"]["issue_context_prompt"].startswith("Fix this benchmark issue.")
    assert "- run_tests" not in payload["prompt_preview"]["tool_use_instructions"]

    run_id = UUID(payload["id"])
    with TestingSessionLocal() as db:
        config_event = db.scalar(
            select(AgentEvent).where(
                AgentEvent.agent_run_id == run_id,
                AgentEvent.event_type == "agent_run_configured",
            )
        )
        assert config_event is not None
        assert config_event.payload_json["config"] == request_payload

    detail_response = client.get(f"/agent-runs/{run_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["run_config"] == request_payload
    assert detail["prompt_preview"] == payload["prompt_preview"]


def create_task(
    *,
    status: str,
    setup_commands: list[str] | None = None,
    test_commands: list[str] | None = None,
    gold_patch_text: str = "diff --git a/src/calculator.py b/src/calculator.py\n",
) -> str:
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
            fix_commit="2222222222222222222222222222222222222222",
            linked_pr_url="https://github.com/example/calculator/pull/43",
            setup_commands=setup_commands or [],
            test_commands=test_commands or [python_command("print('ok')")],
            status=status,
        )
        db.add(task)
        db.flush()
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=["src/calculator.py"],
                patch_text=gold_patch_text,
                test_files=["tests/test_calculator.py"],
            )
        )
        db.commit()
        return str(task.id)


def python_command(code: str) -> str:
    escaped = code.replace('"', '\\"')
    return f'"{sys.executable}" -c "{escaped}"'


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


class ManagedWorkspacePreparer:
    def __init__(self, manager, metadata) -> None:
        self.manager = manager
        self.metadata = metadata

    def prepare(
        self,
        *,
        repository_url: str,
        base_commit: str,
        command_timeout_seconds: int,
    ) -> PreparedWorkspace:
        return PreparedWorkspace(
            workspace_id=self.metadata.workspace_id,
            path=self.metadata.repo_path,
            workspace_path=self.metadata.workspace_path,
            metadata=self.metadata,
            cleanup=lambda: self.manager.cleanup_workspace(self.metadata),
        )
