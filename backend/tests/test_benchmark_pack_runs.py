import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.orchestrator import AgentRunOrchestrator, PreparedWorkspace
from app.api.routes.agent_run_orchestration import (
    get_model_provider_factory,
    get_workspace_preparer,
)
from app.benchmark_packs.runs import BenchmarkPackRunService, calculate_pack_aggregates
from app.core.config import Settings, settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.model_providers import (
    AnthropicProvider,
    LocalModelProvider,
    MockModelProvider,
    ModelProviderFactory,
    ModelProviderResponse,
    ModelToolCall,
    OpenAIProvider,
)
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkPack,
    BenchmarkPackRun,
    BenchmarkPackTask,
    BenchmarkTask,
    GoldPatch,
    HiddenEvalTest,
    Repository,
)
from app.models import TestResult as ResultRecord
from app.schemas.benchmark_pack_run import BenchmarkPackRunRequest

GOLD_MARKER = "PRIVATE_GOLD_SOLUTION"
HIDDEN_MARKER = "PRIVATE_HIDDEN_SUITE"
FIXED_SOURCE = "def add(a, b):\n    return a + b\n"


def python_command(source):
    return f'"{sys.executable}" -B -c "{source}"'


class RecordingMock(MockModelProvider):
    def __init__(self, model_name):
        super().__init__(
            model_name=model_name,
            responses=[
                ModelProviderResponse(
                    content="",
                    tool_calls=[
                        ModelToolCall(
                            id="read", name="read_file", arguments={"file_path": "calculator.py"}
                        )
                    ],
                ),
                ModelProviderResponse(
                    content="",
                    tool_calls=[
                        ModelToolCall(
                            id="write",
                            name="write_file",
                            arguments={"file_path": "calculator.py", "content": FIXED_SOURCE},
                        )
                    ],
                ),
                ModelProviderResponse(
                    content="", tool_calls=[ModelToolCall(id="submit", name="submit_patch")]
                ),
            ],
        )
        self.contexts = []

    def generate_response(self, messages, tools=None):
        self.contexts.append(repr(messages))
        return super().generate_response(messages, tools)


class MockFactory(ModelProviderFactory):
    def __init__(self):
        super().__init__(
            app_settings=Settings(
                _env_file=None,
                ENABLE_REAL_MODEL_CALLS=False,
                ENABLE_LOCAL_MODEL_CALLS=False,
                OPENAI_API_KEY="test-only",
                ANTHROPIC_API_KEY="test-only",
                LOCAL_MODEL_ENDPOINT="http://localhost:9999/v1",
            )
        )
        self.providers = []

    def create(self, provider_name, *, model_name=None):
        if provider_name != "mock":
            return super().create(provider_name, model_name=model_name)
        provider = RecordingMock(model_name)
        self.providers.append(provider)
        return provider


class Workspaces:
    def __init__(self, root):
        self.root = root
        self.paths = []
        self.closed = []

    def prepare(self, **kwargs):
        assert self.paths == self.closed, "previous workspace must be closed before next task"
        path = self.root / uuid4().hex
        path.mkdir()
        self.paths.append(path)
        (path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        for args in (
            ["init"],
            ["config", "user.email", "test@example.com"],
            ["config", "user.name", "Test"],
            ["config", "core.autocrlf", "false"],
            ["add", "."],
            ["commit", "-m", "base"],
        ):
            subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)
        return PreparedWorkspace(
            workspace_id=path.name, path=path, cleanup=lambda: self.closed.append(path)
        )


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def factory():
    return MockFactory()


@pytest.fixture()
def preparer(tmp_path):
    return Workspaces(tmp_path)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Pack-run tests must not call real model APIs.")

    monkeypatch.setattr(OpenAIProvider, "_get_client", forbidden)
    monkeypatch.setattr(AnthropicProvider, "_get_client", forbidden)
    monkeypatch.setattr(LocalModelProvider, "_post", forbidden)


@pytest.fixture()
def client(db, factory, preparer, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "trusted_operator_token", "pack-test-operator")
    monkeypatch.setattr(settings, "sandbox_workspace_root", tmp_path / "hidden-workspaces")
    monkeypatch.setattr(settings, "sandbox_retain_workspaces", False)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_workspace_preparer] = lambda: preparer
    app.dependency_overrides[get_model_provider_factory] = lambda: factory
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def pack(db):
    repo = Repository(name="calculator", owner="example", url="https://github.com/example/calc")
    pack = BenchmarkPack(name="Repairs", slug="repairs-v1", version="1")
    db.add_all([repo, pack])
    db.flush()
    for position, status in [(20, "ready"), (0, "draft"), (10, "ready")]:
        task = BenchmarkTask(
            repository_id=repo.id,
            issue_number=position + 1,
            issue_title=f"Addition issue {position}",
            issue_body="add(2, 3) must equal 5.",
            base_commit="base-commit",
            fix_commit="PRIVATE_FIX_COMMIT",
            status=status,
            setup_commands=[python_command("print('setup')")],
            test_commands=[python_command("from calculator import add; assert add(2, 3) == 5")],
        )
        db.add(task)
        db.flush()
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                patch_text=GOLD_MARKER,
                changed_files=["calculator.py", "PRIVATE_GOLD_FILE.py"],
                test_files=[],
            )
        )
        db.add(
            BenchmarkPackTask(
                benchmark_pack_id=pack.id,
                benchmark_task_id=task.id,
                order_index=position,
            )
        )
    db.commit()
    return pack


def ready_tasks(pack):
    return [
        member.benchmark_task
        for member in pack.task_memberships
        if member.benchmark_task.status == "ready"
    ]


def start(client, pack, **options):
    response = client.post(
        f"/benchmark-packs/{pack.id}/runs",
        json={
            "model_provider": "mock",
            "model_name": "pack-mock",
            "max_steps": 4,
            "command_timeout_seconds": 15,
            "enable_test_tool": False,
            **options,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_pack_runs_ready_tasks_in_order_with_isolated_workspaces(
    client, db, pack, factory, preparer
):
    result = start(client, pack)
    assert result["status"] == "completed", result
    assert [task["order_index"] for task in result["tasks"]] == [10, 20]
    assert [task["benchmark_task_id"] for task in result["tasks"]] == [
        str(task.id) for task in ready_tasks(pack)
    ]
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
    assert len(set(preparer.paths)) == 2 and preparer.closed == preparer.paths
    assert len(factory.providers) == 2
    assert result["run_config"]["model_name"] == "pack-mock"
    assert result["run_config"]["enable_test_tool"] is False
    assert result["include_hidden_tests"] is False
    assert result["run_config"]["run_hidden_tests"] is False
    aggregates = result["aggregates"]
    assert aggregates["total_tasks"] == aggregates["completed_tasks"] == 2
    assert aggregates["failed_tasks"] == aggregates["skipped_tasks"] == 0
    assert aggregates["issue_resolved_count"] == 2
    assert aggregates["issue_resolved_rate"] == aggregates["visible_test_pass_rate"] == 1
    assert aggregates["average_file_localization_score"] == 0.5
    assert aggregates["average_issue_specific_score"] == 0.75
    assert aggregates["hidden_test_pass_rate"] is None
    assert aggregates["total_cost"] == aggregates["total_tokens"] == 0
    assert aggregates["total_execution_time"] > 0
    assert aggregates["average_execution_time"] == pytest.approx(
        aggregates["total_execution_time"] / 2, abs=0.0001
    )
    for item in result["tasks"]:
        run = db.get(AgentRun, UUID(item["agent_run_id"]))
        assert run.generated_patch.patch_text and run.generated_patch.is_selected
        assert run.evaluation_metric.post_patch_tests_passed
        assert {row.phase for row in run.test_results} == {"setup", "baseline", "post_patch"}
        assert {event.event_type for event in run.events} >= {
            "benchmark_pack_task_requested",
            "benchmark_pack_task_started",
            "benchmark_pack_task_finished",
            "agent_run_configured",
        }
        assert item["metric_summary"]["baseline_tests_passed"] is False
    assert client.get(f"/benchmark-pack-runs/{result['id']}").json() == result


@pytest.mark.parametrize("stop", [False, True])
def test_task_failure_continues_or_stops_as_configured(client, db, pack, preparer, stop):
    ready_tasks(pack)[0].setup_commands = [python_command("raise SystemExit(1)")]
    db.commit()
    result = start(client, pack, stop_on_task_failure=stop)
    first, second = result["tasks"]
    assert first["status"] == "failed" and first["failure_category"] == "setup_failed"
    assert first["metric_summary"]["post_patch_tests_passed"] is False
    assert result["aggregates"]["failed_tasks"] == 1
    assert result["aggregates"]["total_tasks"] == 2
    if stop:
        assert second["status"] == "skipped" and second["failure_category"] == "cancelled"
        run = db.get(AgentRun, UUID(second["agent_run_id"]))
        assert run.status == "cancelled" and run.workspace_id is None
        assert len(preparer.paths) == 1
        assert result["status"] == "failed"
        assert result["aggregates"]["skipped_tasks"] == 1
        assert result["aggregates"]["issue_resolved_rate"] == 0
    else:
        assert second["status"] == "completed"
        assert len(preparer.paths) == 2
        assert result["status"] == "partial_failure"
        assert result["aggregates"]["issue_resolved_rate"] == 0.5
        assert result["aggregates"]["average_issue_specific_score"] == 0.375


@pytest.mark.parametrize("enabled", [False, True])
def test_hidden_tests_opt_in_and_gold_data_never_reach_agent(client, db, pack, factory, enabled):
    task = ready_tasks(pack)[0]
    db.add(
        HiddenEvalTest(
            benchmark_task_id=task.id,
            name=HIDDEN_MARKER,
            enabled=True,
            commands=[
                python_command(
                    "from pathlib import Path; from calculator import add; assert add(2, 3) == 5; "
                    f"assert Path('.benchmark-hidden-eval/case.txt').read_text() == '{HIDDEN_MARKER}'"
                )
            ],
            files_payload={"case.txt": HIDDEN_MARKER},
        )
    )
    db.commit()
    options = {"include_hidden_tests": True} if enabled else {}
    response = client.post(
        f"/benchmark-packs/{pack.id}/runs",
        json=options,
        headers={"X-Operator-Token": "pack-test-operator"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "completed", result
    hidden_rows = list(db.scalars(select(ResultRecord).where(ResultRecord.phase == "hidden_eval")))
    assert len(hidden_rows) == (1 if enabled else 0)
    assert result["aggregates"]["hidden_test_pass_rate"] == (1.0 if enabled else None)
    assert result["aggregates"]["hidden_tested_tasks"] == int(enabled)
    assert result["aggregates"]["average_issue_specific_score"] == (0.875 if enabled else 0.75)
    public = response.text
    for item in result["tasks"]:
        public += client.get(f"/agent-runs/{item['agent_run_id']}").text
        public += client.get(f"/agent-runs/{item['agent_run_id']}/tests").text
    public += repr([provider.contexts for provider in factory.providers])
    public += json.dumps([event.payload_json for event in db.scalars(select(AgentEvent))])
    for private in [GOLD_MARKER, HIDDEN_MARKER, "PRIVATE_GOLD_FILE.py", "PRIVATE_FIX_COMMIT"]:
        assert private not in public


@pytest.mark.parametrize("headers", [{}, {"X-Operator-Token": "wrong"}, {"X-Actor": "admin"}])
def test_hidden_tests_require_operator_before_creating_runs(client, db, pack, headers):
    response = client.post(
        f"/benchmark-packs/{pack.id}/runs", json={"include_hidden_tests": True}, headers=headers
    )
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(BenchmarkPackRun)) == 0
    with pytest.raises(PermissionError, match="trusted operator"):
        BenchmarkPackRunService(db).start(
            pack.id, BenchmarkPackRunRequest(include_hidden_tests=True)
        )


@pytest.mark.parametrize("provider", ["openai", "anthropic", "local"])
def test_real_providers_still_obey_feature_flags(client, db, pack, preparer, provider):
    result = start(client, pack, model_provider=provider)
    assert result["status"] == "failed"
    assert result["aggregates"]["failed_tasks"] == 2
    assert all(item["failure_category"] == "model_provider_error" for item in result["tasks"])
    assert preparer.paths == []
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 2


@pytest.mark.parametrize(
    "options",
    [
        {"max_steps": 0},
        {"max_tool_errors": 0},
        {"command_timeout_seconds": 601},
        {"run_hidden_tests": True},
        {"commands": ["arbitrary command"]},
        {"run_mode": "scripted", "model_provider": "openai"},
    ],
)
def test_invalid_configuration_rejected_without_side_effects(client, db, pack, options):
    assert client.post(f"/benchmark-packs/{pack.id}/runs", json=options).status_code == 422
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_missing_pack_run_and_pack_without_ready_tasks(client, db, pack):
    assert client.get(f"/benchmark-pack-runs/{uuid4()}").status_code == 404
    assert client.post(f"/benchmark-packs/{uuid4()}/runs", json={}).status_code == 404
    for task in ready_tasks(pack):
        task.status = "draft"
    db.commit()
    response = client.post(f"/benchmark-packs/{pack.id}/runs", json={})
    assert response.status_code == 409 and "no ready tasks" in response.text
    assert db.scalar(select(func.count()).select_from(BenchmarkPackRun)) == 0


def test_results_are_snapshots_and_repeated_posts_create_fresh_runs(client, db, pack, preparer):
    first = start(client, pack)
    second = start(client, pack)
    assert first["id"] != second["id"] and len(set(preparer.paths)) == 4
    pack.name = "Changed name"
    pack.version = "2"
    for task in ready_tasks(pack):
        task.issue_title = "Changed issue"
    run = db.get(AgentRun, UUID(first["tasks"][0]["agent_run_id"]))
    run.evaluation_metric.issue_resolved = False
    db.delete(pack.task_memberships[1])
    db.commit()
    assert client.get(f"/benchmark-pack-runs/{first['id']}").json() == first


def test_queued_task_definition_change_is_classified_not_executed(db, pack, factory, preparer):
    tasks = ready_tasks(pack)
    orchestrator = AgentRunOrchestrator(
        db=db, provider_factory=factory, workspace_preparer=preparer
    )

    class MutatingRunner:
        def start_run(self, **kwargs):
            result = orchestrator.start_run(**kwargs)
            tasks[1].test_commands = [python_command("print('changed')")]
            db.commit()
            return result

    result = BenchmarkPackRunService(
        db, orchestrator=MutatingRunner(), provider_factory=factory
    ).start(pack.id, BenchmarkPackRunRequest())
    assert result.tasks[0].status == "completed"
    assert result.tasks[1].failure_category == "task_not_ready"
    assert len(preparer.paths) == 1


def test_aggregate_formulas_include_failed_tasks_and_hidden_coverage():
    tasks = [
        SimpleNamespace(
            status="completed",
            metric_summary={
                "issue_resolved": True,
                "post_patch_tests_passed": True,
                "hidden_tests_passed": True,
                "hidden_tests_run_count": 3,
                "file_localization_score": 1,
                "issue_specific_score": 1,
                "tokens_used": 100,
                "estimated_cost": 0.1,
                "execution_time_seconds": 2,
            },
        ),
        SimpleNamespace(
            status="failed",
            metric_summary={
                "hidden_tests_passed": False,
                "hidden_tests_run_count": 1,
                "file_localization_score": 0.5,
                "issue_specific_score": 0,
                "tokens_used": 200,
                "estimated_cost": 0.2,
                "execution_time_seconds": 4,
            },
        ),
        SimpleNamespace(status="skipped", metric_summary=None),
    ]
    result = calculate_pack_aggregates(tasks)
    assert result.total_tasks == 3 and result.skipped_tasks == 1
    assert result.issue_resolved_rate == result.visible_test_pass_rate == pytest.approx(1 / 3)
    assert result.hidden_test_pass_rate == 0.5 and result.hidden_tested_tasks == 2
    assert result.average_file_localization_score == 0.5
    assert result.average_issue_specific_score == pytest.approx(1 / 3)
    assert result.total_tokens == 300 and result.total_cost == 0.3
    assert result.total_execution_time == 6 and result.average_execution_time == 3
    empty = calculate_pack_aggregates([])
    assert empty.total_tasks == 0 and empty.hidden_test_pass_rate is None


def test_unexpected_failure_is_redacted_and_does_not_stop_other_tasks(db, pack, factory, preparer):
    real = AgentRunOrchestrator(db=db, provider_factory=factory, workspace_preparer=preparer)

    class BrokenOnce:
        calls = 0

        def start_run(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("api_key=never-expose-this")
            return real.start_run(**kwargs)

    result = BenchmarkPackRunService(db, orchestrator=BrokenOnce(), provider_factory=factory).start(
        pack.id, BenchmarkPackRunRequest()
    )
    assert result.status == "partial_failure"
    assert result.tasks[0].failure_category == "unknown"
    assert "never-expose-this" not in result.model_dump_json()


def test_evaluation_failure_preserves_usage_and_original_failure(db, pack, factory):
    for task in ready_tasks(pack):
        db.delete(task.gold_patch)
    db.commit()

    class UsageThenFailure:
        def start_run(self, **kwargs):
            run = db.get(AgentRun, kwargs["agent_run_id"])
            run.model_provider = "recorded-provider"
            run.started_at = datetime.now(UTC) - timedelta(seconds=2)
            db.add(
                AgentEvent(
                    agent_run_id=run.id,
                    event_type="model_response",
                    payload_json={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "estimated_cost": 0.01,
                    },
                )
            )
            db.commit()
            raise TypeError("failure")

    result = BenchmarkPackRunService(
        db, orchestrator=UsageThenFailure(), provider_factory=factory
    ).start(pack.id, BenchmarkPackRunRequest())
    assert result.status == "failed"
    assert result.aggregates.total_tokens == 240 and result.aggregates.total_cost == 0.02
    assert result.aggregates.total_execution_time >= 4
    assert all("execution failed" in item.failure_summary for item in result.tasks)
