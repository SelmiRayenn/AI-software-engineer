import json
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.comparison import ModelComparisonService, calculate_aggregates
from app.agents.orchestrator import AgentRunOrchestrator, PreparedWorkspace
from app.api.routes.agent_run_orchestration import (
    get_model_provider_factory,
    get_workspace_preparer,
)
from app.core.config import Settings
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
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    Repository,
)
from app.models import TestResult as ResultRecord
from app.schemas.agent_run import AgentRunStartRequest
from app.schemas.model_comparison import ModelComparisonRequest, ModelComparisonRun

GOLD_TEXT = "GOLD_SOLUTION_MUST_NOT_LEAK"
GOLD_FILE = "hidden_solution_only.py"
BASE_SOURCE = "def add(a, b):\n    return a - b\n"
FIXED_SOURCE = "def add(a, b):\n    return a + b\n"


class RecordingMock(MockModelProvider):
    def __init__(self, model_name: str) -> None:
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
                            id="edit",
                            name="write_file",
                            arguments={
                                "file_path": "calculator.py",
                                "content": FIXED_SOURCE
                                if model_name != "bad-fix"
                                else "def add(a, b):\n    return 0\n",
                            },
                        )
                    ],
                ),
                ModelProviderResponse(
                    content="", tool_calls=[ModelToolCall(id="submit", name="submit_patch")]
                ),
            ],
        )
        self.contexts = []
        self.tools_seen = []

    def generate_response(self, messages, tools=None):
        self.contexts.append([message.content for message in messages])
        self.tools_seen.append([tool.name for tool in tools or []])
        if self.model_name == "broken":
            raise RuntimeError("mock provider failed; api_key=mock-secret")
        return super().generate_response(messages, tools)


class MockComparisonFactory(ModelProviderFactory):
    def __init__(self) -> None:
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


class FreshWorkspacePreparer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = []
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        workspace = self.root / uuid4().hex
        workspace.mkdir()
        self.paths.append(workspace)
        (workspace / "calculator.py").write_text(BASE_SOURCE, encoding="utf-8")
        for args in (
            ["init"],
            ["config", "user.email", "test@example.com"],
            ["config", "user.name", "Test"],
            ["config", "core.autocrlf", "false"],
            ["add", "."],
            ["commit", "-m", "base"],
        ):
            subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
        return PreparedWorkspace(workspace_id=workspace.name, path=workspace)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
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
def factory() -> MockComparisonFactory:
    return MockComparisonFactory()


@pytest.fixture()
def preparer(tmp_path: Path) -> FreshWorkspacePreparer:
    return FreshWorkspacePreparer(tmp_path)


@pytest.fixture()
def client(db, factory, preparer, monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("Comparison tests must not call a real model server.")

    monkeypatch.setattr(OpenAIProvider, "_get_client", forbid_network)
    monkeypatch.setattr(AnthropicProvider, "_get_client", forbid_network)
    monkeypatch.setattr(LocalModelProvider, "_post", forbid_network)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_workspace_preparer] = lambda: preparer
    app.dependency_overrides[get_model_provider_factory] = lambda: factory
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def task(db: Session) -> BenchmarkTask:
    repo = Repository(
        name="calculator", owner="example", url="https://github.com/example/calculator"
    )
    db.add(repo)
    db.flush()
    task = BenchmarkTask(
        repository_id=repo.id,
        issue_number=1,
        issue_title="Addition is incorrect",
        issue_body="add(2, 3) must equal 5.",
        issue_comments=[{"body": "COMMENT_MARKER"}],
        base_commit="base-commit",
        fix_commit="SECRET_FIX_COMMIT",
        status="ready",
        setup_commands=[f'"{sys.executable}" -c "print(123)"'],
        test_commands=[
            f'"{sys.executable}" -B -c "from calculator import add; assert add(2, 3) == 5"'
        ],
    )
    db.add(task)
    db.flush()
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            patch_text=GOLD_TEXT,
            changed_files=["calculator.py", GOLD_FILE],
            test_files=[GOLD_FILE],
        )
    )
    db.commit()
    return task


def payload(*models: str, **options) -> dict:
    return {
        "models": [{"model_provider": "mock", "model_name": name} for name in models],
        "max_steps": 4,
        "max_tool_errors": 2,
        "command_timeout_seconds": 15,
        "include_issue_comments": False,
        "enable_test_tool": False,
        "run_mode": "tool_loop",
        **options,
    }


def test_comparison_runs_real_loop_and_stores_patches_tests_metrics(
    client, db, task, factory, preparer
):
    response = client.post(
        f"/benchmark-tasks/{task.id}/compare-models", json=payload("alpha", "beta")
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", response.text
    assert [run["model"] for run in result["runs"]] == ["alpha", "beta"]
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
    assert len(set(preparer.paths)) == 2
    assert all(
        call
        == {
            "repository_url": task.repository.url,
            "base_commit": task.base_commit,
            "command_timeout_seconds": 15,
        }
        for call in preparer.calls
    )
    for row in result["runs"]:
        assert row["status"] == "completed"
        assert row["patch_applied"] is True
        assert row["tests_passed"] is True
        assert row["file_localization_score"] == 0.5
        assert row["modified_files_count"] == 1
        assert row["unrelated_files_count"] == 0
        assert row["tokens_used"] == 0 and row["estimated_cost"] == 0
        assert row["execution_time_seconds"] >= 0
        run_id = UUID(row["run_id"])
        run = db.get(AgentRun, run_id)
        assert run.generated_patch.patch_text
        assert run.generated_patch.changed_files == ["calculator.py"]
        assert run.evaluation_metric.id == UUID(row["metric_id"])
        assert sorted(test.phase for test in run.test_results) == [
            "baseline",
            "post_patch",
            "setup",
        ]
        assert [test.passed for test in run.test_results if test.phase == "baseline"] == [False]
        assert [test.passed for test in run.test_results if test.phase == "post_patch"] == [True]
        event_types = {event.event_type for event in run.events}
        assert {
            "model_comparison_run_requested",
            "model_comparison_run_started",
            "model_comparison_run_finished",
            "agent_tool_call",
            "model_response",
            "evaluation_metric_calculated",
        } <= event_types
        config = next(
            event.payload_json["config"]
            for event in run.events
            if event.event_type == "model_comparison_run_requested"
        )
        assert config["max_tool_errors"] == 2 and config["command_timeout_seconds"] == 15

    all_data = json.dumps(result) + json.dumps([p.contexts for p in factory.providers])
    all_data += json.dumps([e.payload_json for e in db.scalars(select(AgentEvent))])
    for hidden in (GOLD_TEXT, GOLD_FILE, "SECRET_FIX_COMMIT", "COMMENT_MARKER"):
        assert hidden not in all_data
    assert all("run_tests" not in names for p in factory.providers for names in p.tools_seen)
    assert client.get(f"/benchmark-tasks/{task.id}/model-comparison").json() == result
    assert result["aggregates"]["best_passing_model"]["run_id"] in {
        run["run_id"] for run in result["runs"]
    }


@pytest.mark.parametrize("failing_model", ["broken", "bad-fix"])
def test_failed_model_does_not_stop_other_models(client, db, task, failing_model):
    response = client.post(
        f"/benchmark-tasks/{task.id}/compare-models", json=payload(failing_model, "good")
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "partial_failure"
    failed, good = result["runs"]
    assert failed["status"] == "failed" and failed["failure_reason"]
    assert failed["tests_passed"] is False and failed["metric_id"]
    assert good["status"] == "completed" and good["tests_passed"] is True
    assert "mock-secret" not in json.dumps(result)
    assert "mock-secret" not in json.dumps(
        [event.payload_json for event in db.scalars(select(AgentEvent))]
    )
    assert result["aggregates"]["best_passing_model"]["model"] == "good"
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
    if failing_model == "bad-fix":
        assert failed["patch_applied"] is True
        assert db.scalar(select(func.count()).select_from(GeneratedPatch)) == 2
        assert db.scalar(select(func.count()).select_from(ResultRecord)) == 6


@pytest.mark.parametrize("provider", ["openai", "anthropic", "local"])
def test_provider_flags_are_enforced_and_configuration_failure_is_audited(
    client, db, task, provider
):
    request = payload("good", "blocked-model")
    request["models"][0]["model_provider"] = provider
    response = client.post(f"/benchmark-tasks/{task.id}/compare-models", json=request)
    assert response.status_code == 200, response.text
    failed, good = response.json()["runs"]
    assert failed["status"] == "failed"
    assert "disabled" in failed["failure_reason"]
    assert "ENABLE_" in failed["failure_reason"]
    assert failed["metric_id"]
    assert good["status"] == "completed"
    assert db.get(AgentRun, UUID(failed["run_id"])).workspace_path is None


def test_latest_comparison_and_explicit_history_are_task_scoped(client, db, task):
    first = client.post(f"/benchmark-tasks/{task.id}/compare-models", json=payload("a", "b")).json()
    second = client.post(
        f"/benchmark-tasks/{task.id}/compare-models", json=payload("c", "d")
    ).json()
    assert first["comparison_id"] != second["comparison_id"]
    assert client.get(f"/benchmark-tasks/{task.id}/model-comparison").json() == second
    assert (
        client.get(
            f"/benchmark-tasks/{task.id}/model-comparison",
            params={"comparison_id": first["comparison_id"]},
        ).json()
        == first
    )
    other = BenchmarkTask(
        repository_id=task.repository_id,
        issue_number=2,
        issue_title="Other",
        base_commit="base",
        status="ready",
    )
    db.add(other)
    db.commit()
    assert (
        client.get(
            f"/benchmark-tasks/{other.id}/model-comparison",
            params={"comparison_id": first["comparison_id"]},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "invalid_request",
    [
        payload("only-one"),
        payload("duplicate", "duplicate"),
        payload("a", "b", max_steps=0),
        payload("a", "b", max_tool_errors=0),
        payload("a", "b", command_timeout_seconds=601),
        payload("a", "b", run_mode="shell"),
        payload(
            "a",
            "b",
            models=[
                {"model_provider": "mock", "model_name": " "},
                {"model_provider": "mock", "model_name": "b"},
            ],
        ),
        payload(
            "a",
            "b",
            models=[
                {"model_provider": "local", "model_name": "a"},
                {"model_provider": "mock", "model_name": "b"},
            ],
            run_mode="scripted",
        ),
    ],
)
def test_invalid_requests_create_no_runs(client, db, task, invalid_request):
    response = client.post(f"/benchmark-tasks/{task.id}/compare-models", json=invalid_request)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_missing_and_non_ready_tasks(client, db, task):
    assert (
        client.post(
            f"/benchmark-tasks/{uuid4()}/compare-models", json=payload("a", "b")
        ).status_code
        == 404
    )
    assert client.get(f"/benchmark-tasks/{uuid4()}/model-comparison").status_code == 404
    assert client.get(f"/benchmark-tasks/{task.id}/model-comparison").status_code == 404
    task.status = "draft"
    db.commit()
    assert (
        client.post(
            f"/benchmark-tasks/{task.id}/compare-models", json=payload("a", "b")
        ).status_code
        == 409
    )
    assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def metric_row(name, *, score=1.0, cost=1.0, seconds=10.0, **overrides):
    return ModelComparisonRun(
        **{
            "run_id": uuid4(),
            "provider": "mock",
            "model": name,
            "status": "completed",
            "patch_applied": True,
            "tests_passed": True,
            "file_localization_score": score,
            "modified_files_count": 1,
            "unrelated_files_count": 0,
            "estimated_cost": cost,
            "execution_time_seconds": seconds,
            **overrides,
        }
    )


def test_aggregates_rank_quality_cost_speed_and_include_failed_costs():
    runs = [
        metric_row("quality", score=1, cost=3, seconds=8),
        metric_row("cheap", score=0.5, cost=0.2, seconds=10),
        metric_row("fast", score=0.5, cost=1, seconds=2),
        metric_row("failed", status="failed", cost=2, seconds=5),
        metric_row("noop", patch_applied=False, cost=0, seconds=0),
    ]
    result = calculate_aggregates(runs)
    assert result.best_passing_model.model == "quality"
    assert result.lowest_cost_passing_model.model == "cheap"
    assert result.fastest_passing_model.model == "fast"
    assert result.highest_localization_score == 1
    assert result.total_cost == 6.2
    assert result.total_execution_time == 25


def test_no_winners_without_passing_runs_or_known_cost_and_time():
    result = calculate_aggregates(
        [
            metric_row("failed", status="failed"),
            metric_row("red-tests", tests_passed=False),
            metric_row("noop", patch_applied=False),
        ]
    )
    assert result.best_passing_model is None
    assert result.lowest_cost_passing_model is None and result.fastest_passing_model is None
    unknown = calculate_aggregates([metric_row("unknown", cost=None, seconds=None)])
    assert unknown.lowest_cost_passing_model is None and unknown.fastest_passing_model is None
    assert calculate_aggregates([]).highest_localization_score is None


def test_best_prefers_fewer_unrelated_files_and_ties_are_deterministic():
    runs = [
        metric_row("z"),
        metric_row("a"),
        metric_row("cheap-unrelated", cost=0, unrelated_files_count=1),
    ]
    assert calculate_aggregates(runs).best_passing_model.model == "a"
    assert calculate_aggregates(list(reversed(runs))).best_passing_model.model == "a"


def test_prepared_run_rejects_reuse_and_waiting_time_is_not_execution_time(
    db, task, factory, preparer
):
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="a",
        status="queued",
        started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(run)
    db.commit()
    orchestrator = AgentRunOrchestrator(
        db=db, provider_factory=factory, workspace_preparer=preparer
    )
    request = AgentRunStartRequest(model_provider="mock", model_name="a")
    result = orchestrator.start_run(benchmark_task_id=task.id, request=request, agent_run_id=run.id)
    assert result.status == "completed"
    metric = db.scalar(select(EvaluationMetric).where(EvaluationMetric.agent_run_id == run.id))
    assert metric.execution_time_seconds < 120
    with pytest.raises(RuntimeError, match="Prepared run must be queued"):
        orchestrator.start_run(benchmark_task_id=task.id, request=request, agent_run_id=run.id)


def test_unexpected_run_exception_is_recorded_and_remaining_model_runs(db, task, factory, preparer):
    real = AgentRunOrchestrator(db=db, provider_factory=factory, workspace_preparer=preparer)

    class IntermittentOrchestrator:
        def start_run(self, **kwargs):
            if kwargs["request"].model_name == "exception":
                raise TypeError("sensitive diagnostic should not be returned")
            return real.start_run(**kwargs)

    result = ModelComparisonService(db=db, orchestrator=IntermittentOrchestrator()).compare(
        benchmark_task_id=task.id,
        request=ModelComparisonRequest.model_validate(payload("exception", "good")),
    )
    assert result.runs[0].status == "failed" and result.runs[0].metric_id
    assert result.runs[0].failure_reason == "Model run failed (TypeError)."
    assert result.runs[1].status == "completed"
