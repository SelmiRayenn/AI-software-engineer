import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import AgentLoop
from app.agents.tools import AgentWorkspaceTools
from app.db.base import Base
from app.model_providers import MockModelProvider, ModelProviderResponse, ModelToolCall
from app.models import AgentEvent, AgentRun, BenchmarkTask, GeneratedPatch, GoldPatch, Repository
from app.repository_indexing import RelevantFilesResult
from app.schemas.agent_run import AgentRunConfig

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
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    init_git_repo(root)
    return root


def test_loop_executes_mock_tool_calls_and_logs_events(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[
            response("list_files", "call-list"),
            response("read_file", "call-read", {"file_path": "README.md"}),
            response("submit_patch", "call-submit"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=5)

    result = loop.run()

    assert result.stop_reason == "patch_submitted"
    assert [step.step_name for step in result.steps] == [
        "list_files",
        "read_file",
        "submit_patch",
    ]
    assert result.submitted_patch is not None
    assert [message.role for message in result.messages].count("tool") == 3
    assert db.scalar(select(GeneratedPatch)) is not None
    event_types = [event.event_type for event in db.scalars(select(AgentEvent)).all()]
    assert event_types.count("model_call_started") == 3
    assert event_types.count("model_call_completed") == 3
    assert event_types.count("tool_call_requested") == 3
    assert event_types.count("tool_call_completed") == 3
    assert "patch_submitted" in event_types
    assert "retrieve_relevant_files" in loop.registered_tool_names()
    assert loop.tool_definitions()[0].name == "retrieve_relevant_files"


def test_unknown_tool_is_rejected(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(responses=[response("delete_repository", "call-unknown")])
    loop = create_loop(
        db,
        workspace,
        provider=provider,
        max_steps=3,
        max_tool_errors=1,
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert result.steps[0].success is False
    assert result.steps[0].error_message == "Unknown tool: delete_repository"
    assert result.failure_category == "unknown_tool"
    failed_event = event_by_type(db, "tool_call_failed")
    assert failed_event.payload_json["tool_name"] == "delete_repository"


def test_loop_dispatches_semantic_retrieval_and_observes_fallback(db, workspace, monkeypatch):
    retrieve = Mock(
        return_value=RelevantFilesResult(
            query="wallet", files=[], fallback_reason="embeddings_unavailable"
        )
    )
    monkeypatch.setattr("app.repository_indexing.RepositoryRetrievalService.retrieve", retrieve)
    provider = MockModelProvider(
        responses=[
            response("retrieve_relevant_files", "retrieve", {"query": "wallet", "semantic": True}),
            response("submit_patch", "submit"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider)
    result = loop.run()
    assert result.stop_reason == "patch_submitted"
    retrieve.assert_called_once_with("wallet", limit=10, semantic=True)
    assert result.steps[0].summary["fallback_reason"] == "embeddings_unavailable"
    assert any(
        "embeddings_unavailable" in message.content
        for message in result.messages
        if message.role == "tool"
    )


def test_malformed_tool_call_is_rejected(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[response("read_file", "call-malformed", {"wrong": "README.md"})]
    )
    loop = create_loop(
        db,
        workspace,
        provider=provider,
        max_steps=3,
        max_tool_errors=1,
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert "Unexpected arguments for read_file" in result.steps[0].error_message
    assert result.failure_category == "malformed_tool_call"


def test_submit_patch_stops_before_later_mock_responses(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[
            response("submit_patch", "call-submit"),
            response("delete_repository", "call-never-used"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=5)

    result = loop.run()

    assert result.stop_reason == "patch_submitted"
    assert result.model_calls == 1
    assert [step.step_name for step in result.steps] == ["submit_patch"]


def test_max_steps_stops_loop_and_logs_event(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[
            response("list_files", "call-1"),
            response("get_diff", "call-2"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=2)

    result = loop.run()

    assert result.stop_reason == "max_steps"
    assert len(result.steps) == 2
    assert result.error_message == "Maximum step limit of 2 reached."
    assert result.failure_category == "max_steps_reached"
    assert event_by_type(db, "step_limit_reached") is not None


def test_max_tool_errors_stops_loop(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[
            response("unknown_one", "call-1"),
            response("unknown_two", "call-2"),
        ]
    )
    loop = create_loop(
        db,
        workspace,
        provider=provider,
        max_steps=5,
        max_tool_errors=2,
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert result.tool_errors == 2
    assert result.model_calls == 2
    assert len(result.steps) == 2
    assert result.failure_category == "unknown_tool"


def test_provider_error_returns_failure_category(db: Session, workspace: Path) -> None:
    provider = MockModelProvider()
    provider.generate_response = Mock(side_effect=RuntimeError("provider offline"))
    loop = create_loop(db, workspace, provider=provider)

    result = loop.run()

    assert result.stop_reason == "provider_error"
    assert result.failure_category == "model_provider_error"


def test_disabled_test_tool_is_not_advertised_or_executable(
    db: Session,
    workspace: Path,
) -> None:
    provider = MockModelProvider(
        responses=[response("run_tests", "call-test", {"command": "pytest"})]
    )
    config = AgentRunConfig(
        model_provider="mock",
        model_name="loop-test",
        max_steps=2,
        max_tool_errors=1,
        enable_test_tool=False,
    )
    loop = create_loop(db, workspace, provider=provider, config=config)

    result = loop.run()

    assert "run_tests" not in loop.registered_tool_names()
    assert "run_tests" not in [definition.name for definition in loop.tool_definitions()]
    assert result.stop_reason == "max_tool_errors"
    assert result.steps[0].error_message == "Unknown tool: run_tests"


def test_initial_context_excludes_gold_solution_data(db: Session, workspace: Path) -> None:
    gold_secret = "HIDDEN-GOLD-PATCH-CONTENT"
    loop = create_loop(db, workspace, gold_patch_text=gold_secret)

    serialized_messages = json.dumps(
        [
            {"role": message.role, "content": message.content}
            for message in loop.build_initial_messages()
        ]
    )

    assert "Fix calculator" in serialized_messages
    assert "https://github.com/example/calculator" in serialized_messages
    assert gold_secret not in serialized_messages
    assert "gold/solution.py" not in serialized_messages
    assert "never approve, export, publish" in serialized_messages.lower()


def test_loop_event_payloads_redact_common_secrets(db: Session, workspace: Path) -> None:
    provider = MockModelProvider(
        responses=[
            ModelProviderResponse(
                content="Use Bearer abcdefghijklmnop and sk-example123456789.",
                tool_calls=[
                    ModelToolCall(
                        id="call-secret",
                        name="unknown_tool",
                        arguments={"api_key": "sk-example123456789"},
                    )
                ],
            )
        ]
    )
    loop = create_loop(
        db,
        workspace,
        provider=provider,
        max_steps=2,
        max_tool_errors=1,
    )

    loop.run()

    serialized_events = json.dumps(
        [event.payload_json for event in db.scalars(select(AgentEvent)).all()]
    )
    assert "abcdefghijklmnop" not in serialized_events
    assert "sk-example123456789" not in serialized_events
    assert "[REDACTED]" in serialized_events


def create_loop(
    db: Session,
    workspace: Path,
    *,
    provider: MockModelProvider | None = None,
    config: AgentRunConfig | None = None,
    max_steps: int = 4,
    max_tool_errors: int = 3,
    gold_patch_text: str = "hidden patch",
) -> AgentLoop:
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
        issue_body="The add function is incorrect.",
        issue_comments=[{"author": "maintainer", "body": "Please add a test."}],
        pull_request_number=43,
        base_commit="1111111111111111111111111111111111111111",
        setup_commands=[],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=["gold/solution.py"],
            patch_text=gold_patch_text,
            test_files=["gold/test_solution.py"],
        )
    )
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="loop-test",
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    tools = AgentWorkspaceTools(
        db=db,
        agent_run_id=run.id,
        workspace_path=workspace,
        allowed_test_commands=task.test_commands,
    )
    return AgentLoop(
        db=db,
        agent_run=run,
        benchmark_task=task,
        repository=repository,
        provider=provider or MockModelProvider(),
        tools=tools,
        config=config,
        max_steps=max_steps,
        max_tool_errors=max_tool_errors,
    )


def response(
    tool_name: str,
    call_id: str,
    arguments: dict | None = None,
) -> ModelProviderResponse:
    return ModelProviderResponse(
        content=f"Calling {tool_name}.",
        tool_calls=[
            ModelToolCall(
                id=call_id,
                name=tool_name,
                arguments=arguments or {},
            )
        ],
        input_tokens=10,
        output_tokens=5,
        estimated_cost=0.0,
    )


def event_by_type(db: Session, event_type: str) -> AgentEvent:
    event = db.scalar(select(AgentEvent).where(AgentEvent.event_type == event_type))
    assert event is not None
    return event


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
