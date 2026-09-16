from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.orchestrator import PreparedWorkspace
from app.agents.prompts import render_agent_prompts
from app.api.routes.agent_run_orchestration import (
    get_model_provider_factory,
    get_workspace_preparer,
)
from app.benchmark_tasks import to_agent_visible_task
from app.core.config import settings
from app.core.test_phases import TEST_PHASE_HIDDEN_EVAL, TEST_PHASE_POST_PATCH
from app.db.base import Base
from app.db.session import get_db
from app.evaluation import EvaluationService
from app.main import create_app
from app.model_providers import MockModelProvider, ModelProviderResponse, ModelToolCall
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    GeneratedPatch,
    GoldPatch,
    HiddenEvalTest,
    Repository,
)
from app.models import (
    TestResult as ResultRecord,
)
from app.patches import PatchService
from app.test_execution import TestExecutionError as ExecutionError
from app.test_execution import TestExecutionSafetyError as ExecutionSafetyError
from app.test_execution import TestExecutionService as ExecutionService

OPERATOR_TOKEN = "trusted-test-token"
HIDDEN_MARKER = "HIDDEN_EVAL_ONLY_MARKER"

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


@pytest.fixture()
def client(db: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(settings, "trusted_operator_token", OPERATOR_TOKEN)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def task_and_run(db: Session, tmp_path: Path, monkeypatch) -> tuple[BenchmarkTask, AgentRun, Path]:
    monkeypatch.setattr(settings, "sandbox_workspace_root", str(tmp_path / "sandboxes"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    for args in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["config", "core.autocrlf", "false"],
        ["add", "."],
        ["commit", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
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
        issue_title="Fix addition",
        issue_body="Addition is incorrect.",
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=["calculator.py"],
            patch_text="gold patch must remain hidden",
            test_files=[],
        )
    )
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="mock",
        status="running",
        workspace_path=str(workspace),
    )
    db.add(run)
    db.flush()
    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text="",
        changed_files=[],
        is_selected=True,
    )
    db.add(patch)
    db.commit()
    return task, run, workspace


def operator_headers() -> dict[str, str]:
    return {"X-Operator-Token": OPERATOR_TOKEN}


def test_trusted_hidden_test_crud_requires_operator(
    client: TestClient,
    task_and_run: tuple[BenchmarkTask, AgentRun, Path],
) -> None:
    task, _, _ = task_and_run
    payload = {
        "name": "boundary behavior",
        "commands": [python_command("print('hidden')")],
        "files_payload": {"case.txt": HIDDEN_MARKER},
    }

    forbidden = client.post(f"/benchmark-tasks/{task.id}/hidden-tests", json=payload)
    assert forbidden.status_code == 403

    created = client.post(
        f"/benchmark-tasks/{task.id}/hidden-tests",
        json=payload,
        headers=operator_headers(),
    )
    assert created.status_code == 201, created.text
    hidden_test_id = UUID(created.json()["id"])
    assert created.json()["files_payload"]["case.txt"] == HIDDEN_MARKER

    listed = client.get(f"/benchmark-tasks/{task.id}/hidden-tests", headers=operator_headers())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(hidden_test_id)]

    deleted = client.delete(f"/hidden-tests/{hidden_test_id}", headers=operator_headers())
    assert deleted.status_code == 204
    assert (
        client.get(f"/benchmark-tasks/{task.id}/hidden-tests", headers=operator_headers()).json()
        == []
    )


def test_hidden_tests_do_not_leak_to_agent_visible_models_or_prompts(
    task_and_run: tuple[BenchmarkTask, AgentRun, Path],
    db: Session,
) -> None:
    task, _, _ = task_and_run
    task.hidden_eval_tests.append(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name=HIDDEN_MARKER,
            commands=[f"echo {HIDDEN_MARKER}"],
            files_payload={"private.txt": HIDDEN_MARKER},
        )
    )
    db.commit()

    prompts = render_agent_prompts(
        task=task,
        repository=task.repository,
        allowed_tools=["list_files", "submit_patch"],
        configured_test_commands=[],
        max_steps=4,
        max_tool_errors=3,
        command_timeout_seconds=120,
        include_issue_comments=False,
        enable_test_tool=False,
        run_mode="tool_loop",
    )
    visible_task = to_agent_visible_task(task)
    rendered = "\n".join(message.content for message in prompts.messages())

    assert HIDDEN_MARKER not in rendered
    assert HIDDEN_MARKER not in json.dumps(prompts.redacted_preview())
    assert HIDDEN_MARKER not in visible_task.model_dump_json()
    assert "hidden_eval_tests" not in visible_task.model_dump()


def test_hidden_evaluation_runs_in_staging_and_metrics_are_separate(
    client: TestClient,
    task_and_run: tuple[BenchmarkTask, AgentRun, Path],
    db: Session,
) -> None:
    task, run, workspace = task_and_run
    task.hidden_eval_tests.append(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name="hidden behavior",
            commands=[
                python_command(
                    "from pathlib import Path; "
                    "assert Path('.benchmark-hidden-eval/case.txt').read_text() == "
                    f"'{HIDDEN_MARKER}'"
                )
            ],
            files_payload={"case.txt": HIDDEN_MARKER},
        )
    )
    db.commit()

    service = ExecutionService(db=db, agent_run_id=run.id)
    result = service.run_hidden_evaluation(
        generated_patch_id=run.generated_patch.id, run_hidden_tests=True
    )

    assert result.phase == TEST_PHASE_HIDDEN_EVAL and result.passed is True
    hidden_rows = list(
        db.scalars(
            select(ResultRecord).where(
                ResultRecord.agent_run_id == run.id,
                ResultRecord.phase == TEST_PHASE_HIDDEN_EVAL,
            )
        )
    )
    assert len(hidden_rows) == 1 and hidden_rows[0].passed is True
    assert not (workspace / ".benchmark-hidden-eval").exists()
    assert list(Path(settings.sandbox_workspace_root).iterdir()) == []

    db.add(
        ResultRecord(
            agent_run_id=run.id,
            generated_patch_id=run.generated_patch.id,
            phase=TEST_PHASE_POST_PATCH,
            command="pytest",
            passed=True,
            exit_code=0,
        )
    )
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    db.commit()

    metric = EvaluationService(db=db, agent_run_id=run.id).evaluate()
    assert metric.tests_passed is True
    assert metric.hidden_tests_passed is True
    assert metric.hidden_tests_run_count == 1
    assert metric.hidden_tests_failed_count == 0

    normal_results = client.get(f"/agent-runs/{run.id}/tests")
    assert normal_results.status_code == 200
    assert all(item["phase"] != TEST_PHASE_HIDDEN_EVAL for item in normal_results.json())
    assert HIDDEN_MARKER not in normal_results.text
    assert all(item.phase != TEST_PHASE_HIDDEN_EVAL for item in service.list_results())
    trusted_results = client.get(
        f"/agent-runs/{run.id}/tests/hidden-eval", headers=operator_headers()
    )
    assert trusted_results.status_code == 200
    assert len(trusted_results.json()) == 1
    assert HIDDEN_MARKER in trusted_results.text


def test_hidden_evaluation_is_not_available_from_public_test_command_api(
    task_and_run: tuple[BenchmarkTask, AgentRun, Path],
    db: Session,
) -> None:
    _, run, _ = task_and_run
    service = ExecutionService(db=db, agent_run_id=run.id)

    with pytest.raises(ExecutionSafetyError, match="Only test phases"):
        service.run_test_command(phase=TEST_PHASE_HIDDEN_EVAL, command="pytest")


@pytest.mark.parametrize("header", [{}, {"X-Operator-Token": "wrong"}, {"X-Actor": "admin"}])
def test_all_trusted_routes_reject_unauthenticated_access(client, task_and_run, header):
    task, run, _ = task_and_run
    urls = [
        ("get", f"/benchmark-tasks/{task.id}/hidden-tests"),
        ("delete", f"/hidden-tests/{uuid4()}"),
        ("get", f"/agent-runs/{run.id}/tests/hidden-eval"),
    ]
    for method, url in urls:
        assert getattr(client, method)(url, headers=header).status_code == 403
    assert (
        client.post(
            f"/agent-runs/{task.id}/start-trusted", json={"run_hidden_tests": True}, headers=header
        ).status_code
        == 403
    )


def test_trusted_routes_disabled_without_token(client, task_and_run, monkeypatch):
    task, _, _ = task_and_run
    monkeypatch.setattr(settings, "trusted_operator_token", None)
    assert (
        client.get(
            f"/benchmark-tasks/{task.id}/hidden-tests", headers=operator_headers()
        ).status_code
        == 503
    )


@pytest.mark.parametrize(
    "path", ["../escape.py", "/escape.py", "C:/escape.py", "a\\b.py", "a/../../x", "a:stream", "."]
)
def test_unsafe_payload_paths_rejected_on_create(client, task_and_run, path):
    task, _, _ = task_and_run
    response = client.post(
        f"/benchmark-tasks/{task.id}/hidden-tests",
        headers=operator_headers(),
        json={"name": "test", "commands": ["pytest"], "files_payload": {path: "data"}},
    )
    assert response.status_code == 422


def test_explicit_backend_opt_in_and_disabled_suites(task_and_run, db):
    task, run, _ = task_and_run
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id, name="disabled", commands=["exit 1"], enabled=False
        )
    )
    db.commit()
    service = ExecutionService(db=db, agent_run_id=run.id)
    with pytest.raises(ExecutionSafetyError, match="opt-in"):
        service.run_hidden_evaluation()
    result = service.run_hidden_evaluation(run_hidden_tests=True)
    assert result.test_results == []
    run.status = "completed"
    db.commit()
    metric = EvaluationService(db=db, agent_run_id=run.id).evaluate()
    assert metric.hidden_tests_passed is None
    assert metric.hidden_tests_run_count == metric.hidden_tests_failed_count == 0


def test_suites_are_isolated_and_hidden_failures_do_not_change_normal_metrics(task_and_run, db):
    task, run, workspace = task_and_run
    for content, code in [("first", 1), ("second", 0)]:
        db.add(
            HiddenEvalTest(
                benchmark_task_id=task.id,
                name=content,
                commands=[
                    python_command(
                        "from pathlib import Path; "
                        f"assert Path('.benchmark-hidden-eval/case.txt').read_text() == '{content}'; "
                        "assert 'return a + b' in Path('calculator.py').read_text(); "
                        "Path('calculator.py').write_text('changed by hidden test'); "
                        f"print('{HIDDEN_MARKER}'); raise SystemExit({code})"
                    )
                ],
                files_payload={"case.txt": content},
            )
        )
    db.commit()
    result = ExecutionService(db=db, agent_run_id=run.id).run_hidden_evaluation(
        run_hidden_tests=True
    )
    assert [row.passed for row in result.test_results] == [False, True]
    assert "return a + b" in (workspace / "calculator.py").read_text()
    assert PatchService(db=db, agent_run_id=run.id).get_current_workspace_diff().patch_text == ""
    assert list(Path(settings.sandbox_workspace_root).iterdir()) == []
    db.add(
        ResultRecord(
            agent_run_id=run.id,
            generated_patch_id=run.generated_patch.id,
            phase=TEST_PHASE_POST_PATCH,
            command="pytest",
            passed=True,
            exit_code=0,
        )
    )
    run.status = "completed"
    db.commit()
    service = EvaluationService(db=db, agent_run_id=run.id)
    metric = service.evaluate()
    assert metric.tests_passed is True and metric.hidden_tests_passed is False
    assert metric.hidden_tests_run_count == 2 and metric.hidden_tests_failed_count == 1
    assert service.evaluate().id == metric.id


def test_unselected_patch_cannot_receive_hidden_results(task_and_run, db):
    _, run, _ = task_and_run
    patch = GeneratedPatch(agent_run_id=run.id, version=2, patch_text="", changed_files=[])
    db.add(patch)
    db.commit()
    with pytest.raises(ExecutionSafetyError, match="selected patch"):
        ExecutionService(db=db, agent_run_id=run.id).run_hidden_evaluation(
            generated_patch_id=patch.id, run_hidden_tests=True
        )


def test_infrastructure_errors_do_not_leak_hidden_payloads(task_and_run, db):
    task, run, _ = task_and_run
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name="invalid backend payload",
            commands=["pytest"],
            files_payload={f"../{HIDDEN_MARKER}": "secret"},
        )
    )
    db.commit()
    with pytest.raises(ExecutionError) as error:
        ExecutionService(db=db, agent_run_id=run.id).run_hidden_evaluation(run_hidden_tests=True)
    assert HIDDEN_MARKER not in str(error.value)
    assert list(Path(settings.sandbox_workspace_root).glob("hidden-eval-*")) == []


def test_partial_hidden_evaluation_cannot_report_success(task_and_run, db):
    _, run, _ = task_and_run
    db.add(
        ResultRecord(
            agent_run_id=run.id,
            generated_patch_id=run.generated_patch.id,
            phase=TEST_PHASE_HIDDEN_EVAL,
            command="pytest",
            passed=True,
            exit_code=0,
        )
    )
    run.status = "failed"
    db.commit()
    metric = EvaluationService(db=db, agent_run_id=run.id).evaluate(include_failed=True)
    assert metric.hidden_tests_passed is False
    assert metric.hidden_tests_run_count == 1


def test_timeout_keeps_bounded_private_logs_and_cleans_workspace(task_and_run, db, monkeypatch):
    task, run, workspace = task_and_run
    command = "hidden-timeout-command"
    db.add(HiddenEvalTest(benchmark_task_id=task.id, name="timeout", commands=[command]))
    db.commit()
    real_run = subprocess.run

    def execute(args, **kwargs):
        if args != command:
            return real_run(args, **kwargs)
        assert kwargs["timeout"] == 3
        assert kwargs["cwd"] != workspace
        raise subprocess.TimeoutExpired(args, 3, output="x" * 1000, stderr=HIDDEN_MARKER)

    monkeypatch.setattr(subprocess, "run", execute)
    result = ExecutionService(
        db=db, agent_run_id=run.id, command_timeout_seconds=3, max_log_bytes=100
    ).run_hidden_evaluation(run_hidden_tests=True)
    assert not result.passed
    row = result.test_results[0]
    assert row.exit_code == 124 and "truncated" in row.stdout and len(row.stdout) < 150
    assert HIDDEN_MARKER in row.stderr
    assert list(Path(settings.sandbox_workspace_root).iterdir()) == []


def test_execution_exception_cleans_private_copy(task_and_run, db, monkeypatch):
    task, run, workspace = task_and_run
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name="error",
            commands=["private command"],
            files_payload={"secret.txt": HIDDEN_MARKER},
        )
    )
    db.commit()
    service = ExecutionService(db=db, agent_run_id=run.id)

    def execute(**kwargs):
        repo = kwargs["workspace_path"]
        assert (repo / ".benchmark-hidden-eval/secret.txt").read_text() == HIDDEN_MARKER
        raise RuntimeError(HIDDEN_MARKER)

    monkeypatch.setattr(service, "_run_command", execute)
    with pytest.raises(ExecutionError) as error:
        service.run_hidden_evaluation(run_hidden_tests=True)
    assert HIDDEN_MARKER not in str(error.value)
    assert list(Path(settings.sandbox_workspace_root).iterdir()) == []
    assert not (workspace / ".benchmark-hidden-eval").exists()


def test_hidden_data_unavailable_to_controlled_tools(task_and_run, db):
    from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
    from app.repository_indexing.workspace import searchable_path

    _, run, workspace = task_and_run
    directory = workspace / ".benchmark-hidden-eval"
    directory.mkdir()
    (directory / "private.txt").write_text(HIDDEN_MARKER)
    tools = AgentWorkspaceTools(db=db, agent_run_id=run.id, workspace_path=workspace)
    assert ".benchmark-hidden-eval/private.txt" not in tools.list_files().files
    with pytest.raises(ToolSafetyError):
        tools.read_file(".benchmark-hidden-eval/private.txt")
    assert not searchable_path(".benchmark-hidden-eval/private.txt")


@pytest.mark.parametrize(
    "trusted,enabled,expected", [(False, True, 0), (True, False, 0), (True, True, 1)]
)
def test_orchestrated_hidden_tests_use_final_patch_and_never_enter_model_context(
    client, task_and_run, db, trusted, enabled, expected
):
    task, _, workspace = task_and_run
    task.test_commands = [python_command("from calculator import add; assert add(2, 3) == 5")]
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name=HIDDEN_MARKER,
            enabled=enabled,
            commands=[
                python_command(
                    "from calculator import add; from pathlib import Path; "
                    "assert add(2, 3) == 5; "
                    f"assert Path('.benchmark-hidden-eval/case.txt').read_text() == '{HIDDEN_MARKER}'; "
                    f"print('{HIDDEN_MARKER}')"
                )
            ],
            files_payload={"case.txt": HIDDEN_MARKER},
        )
    )
    db.commit()

    def response(name, **arguments):
        return ModelProviderResponse(
            content="", tool_calls=[ModelToolCall(id=str(uuid4()), name=name, arguments=arguments)]
        )

    class ObservingMock(MockModelProvider):
        calls = 0

        def generate_response(self, messages, tools=None):
            self.calls += 1
            assert HIDDEN_MARKER not in repr(messages)
            assert not (workspace / ".benchmark-hidden-eval").exists()
            return super().generate_response(messages, tools)

    provider = ObservingMock(
        responses=[
            response(
                "write_file",
                file_path="calculator.py",
                content="def add(a, b):\n    return b + a\n",
            ),
            response("submit_patch"),
            response(
                "write_file", file_path="calculator.py", content="def add(a, b):\n    return 0\n"
            ),
            response("submit_patch"),
        ]
    )
    client.app.dependency_overrides[get_workspace_preparer] = lambda: SimpleNamespace(
        prepare=lambda **kwargs: PreparedWorkspace(workspace_id="test-hidden", path=workspace)
    )
    client.app.dependency_overrides[get_model_provider_factory] = lambda: SimpleNamespace(
        create=lambda *args, **kwargs: provider
    )
    endpoint = "start-trusted" if trusted else "start"
    config = {"max_steps": 8, "max_repair_attempts": 1, "stop_on_first_passing_patch": False}
    if trusted:
        config["run_hidden_tests"] = True
    result = client.post(
        f"/agent-runs/{task.id}/{endpoint}", json=config, headers=operator_headers()
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["status"] == "completed", result.text
    assert HIDDEN_MARKER not in result.text
    assert provider.calls == 4
    run = db.get(AgentRun, UUID(data["id"]))
    assert len(run.generated_patches) == 2
    assert run.final_patch_id == run.generated_patches[0].id
    hidden_results = list(
        db.scalars(
            select(ResultRecord).where(
                ResultRecord.agent_run_id == run.id, ResultRecord.phase == TEST_PHASE_HIDDEN_EVAL
            )
        )
    )
    assert len(hidden_results) == expected
    assert all(
        row.generated_patch_id == run.final_patch_id and row.passed for row in hidden_results
    )
    assert run.evaluation_metric.hidden_tests_run_count == expected
    assert run.evaluation_metric.hidden_tests_passed == (True if expected else None)
    assert run.evaluation_metric.tests_passed is True
    for url in [
        f"/agent-runs/{run.id}",
        f"/agent-runs/{run.id}/tests",
        f"/agent-runs/{run.id}/patch",
        f"/agent-runs/{run.id}/diff",
        f"/agent-runs/{run.id}/metrics",
        "/api/v1/benchmark-tasks",
    ]:
        public = client.get(url)
        assert public.status_code == 200, public.text
        assert HIDDEN_MARKER not in public.text
    events = list(db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run.id)))
    assert HIDDEN_MARKER not in json.dumps([event.payload_json for event in events])
    assert not (workspace / ".benchmark-hidden-eval").exists()


def python_command(source: str) -> str:
    return f'"{sys.executable}" -B -c "{source}"'
