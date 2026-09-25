import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import AgentLoop
from app.agents.tools import AgentWorkspaceTools
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.model_providers import MockModelProvider, ModelProviderResponse, ModelToolCall
from app.models import AgentEvent, AgentRun, BenchmarkTask, GeneratedPatch, GoldPatch, Repository
from app.repository_indexing import RelevantFile, RelevantFilesResult
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
            response("submit_plan", "call-plan", plan_arguments()),
            response("submit_hypothesis", "call-hypothesis", hypothesis_arguments()),
            response("submit_patch", "call-submit"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=6)

    result = loop.run()

    assert result.stop_reason == "patch_submitted"
    assert [step.step_name for step in result.steps] == [
        "list_files",
        "read_file",
        "submit_plan",
        "submit_hypothesis",
        "submit_patch",
    ]
    assert result.submitted_patch is not None
    assert [message.role for message in result.messages].count("tool") == 5
    assert db.scalar(select(GeneratedPatch)) is not None
    event_types = [event.event_type for event in db.scalars(select(AgentEvent)).all()]
    assert event_types.count("model_call_started") == 5
    assert event_types.count("model_call_completed") == 5
    assert event_types.count("tool_call_requested") == 5
    assert event_types.count("tool_call_completed") == 5
    assert "plan_submitted" in event_types
    assert "hypothesis_submitted" in event_types
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
            response("read_file", "read", {"file_path": "README.md"}),
            response("submit_plan", "plan", plan_arguments()),
            response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
            response("submit_patch", "submit"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=5)
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
            response("read_file", "read", {"file_path": "README.md"}),
            response("submit_plan", "plan", plan_arguments()),
            response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
            response("submit_patch", "call-submit"),
            response("delete_repository", "call-never-used"),
        ]
    )
    loop = create_loop(db, workspace, provider=provider, max_steps=5)

    result = loop.run()

    assert result.stop_reason == "patch_submitted"
    assert result.model_calls == 4
    assert [step.step_name for step in result.steps] == [
        "read_file",
        "submit_plan",
        "submit_hypothesis",
        "submit_patch",
    ]


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
    assert "submit_hypothesis" in serialized_messages
    assert "active or confirmed root-cause hypothesis" in serialized_messages


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


def plan_arguments(**changes):
    return {
        "issue_summary": "Fix the calculator issue.",
        "suspected_root_cause": "The addition implementation needs inspection.",
        "files_inspected": ["README.md"],
        "files_likely_to_modify": ["src/calculator.py"],
        "test_strategy": "Run configured pytest tests.",
        "risk_rollback_notes": "Small targeted change; revert the generated diff if tests fail.",
        **changes,
    }


def hypothesis_arguments(**changes):
    return {
        "summary": "The calculator implementation likely contains the reported defect.",
        "suspected_files": ["README.md"],
        "supporting_evidence": ["README.md was inspected during this run."],
        "confidence": "medium",
        "status": "active",
        **changes,
    }


def candidate_arguments(*paths, **changes):
    ranked_files = [
        {
            "path": path,
            "reason": f"Evidence indicates {path} is relevant to the reported behavior.",
            "confidence": "medium",
        }
        for path in (paths or ("README.md",))
    ]
    return {"ranked_files": ranked_files, **changes}


def test_missing_candidate_files_blocks_first_write(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("write_file", "blocked", {"file_path": "README.md", "content": "blocked"}),
            ]
        ),
        config=AgentRunConfig(require_plan_before_edit=False, max_tool_errors=1),
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert "submit_candidate_files" in result.steps[-1].error_message
    assert (workspace / "README.md").read_text() == "# Example\n"


def test_valid_candidate_files_are_stored_and_allow_write(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_candidate_files", "candidates", candidate_arguments()),
                response(
                    "write_file", "write", {"file_path": "README.md", "content": "# Ranked\n"}
                ),
            ]
        ),
        config=AgentRunConfig(require_plan_before_edit=False, max_steps=3),
    )

    result = loop.run()

    assert [step.success for step in result.steps] == [True, True, True]
    assert (workspace / "README.md").read_text() == "# Ranked\n"
    event = event_by_type(db, "candidate_files_submitted")
    assert event.payload_json["ranked_files"][0]["path"] == "README.md"
    assert event.payload_json["ranked_files"][0]["confidence"] == "medium"


def test_candidate_file_limit_is_enforced(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response(
                    "submit_candidate_files",
                    "too-many",
                    candidate_arguments("README.md", "src/calculator.py"),
                ),
            ]
        ),
        config=AgentRunConfig(max_candidate_files=1, max_tool_errors=1),
    )

    result = loop.run()

    assert "at most 1" in result.steps[-1].error_message
    assert (
        db.scalar(select(AgentEvent).where(AgentEvent.event_type == "candidate_files_submitted"))
        is None
    )


@pytest.mark.parametrize("path", ["../outside.py", ".gold/solution.py", "C:\\outside.py"])
def test_unsafe_candidate_paths_are_rejected_without_raw_logging(db, workspace, path):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[response("submit_candidate_files", "unsafe", candidate_arguments(path))]
        ),
        config=AgentRunConfig(max_tool_errors=1),
    )

    result = loop.run()

    assert result.tool_errors == 1
    assert (
        db.scalar(select(AgentEvent).where(AgentEvent.event_type == "candidate_files_submitted"))
        is None
    )
    assert path not in json.dumps([event.payload_json for event in db.scalars(select(AgentEvent))])


def test_candidate_must_have_read_or_retrieval_evidence(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("search_code", "search", {"query": "calculator"}),
                response(
                    "submit_candidate_files",
                    "unsupported",
                    candidate_arguments("src/calculator.py"),
                ),
            ]
        ),
        config=AgentRunConfig(max_tool_errors=1),
    )

    result = loop.run()

    assert "lacks read or retrieval evidence" in result.steps[-1].error_message


def test_candidate_ranking_is_frozen_after_first_write(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_candidate_files", "initial", candidate_arguments()),
                response(
                    "write_file", "write", {"file_path": "README.md", "content": "# Changed\n"}
                ),
                response("submit_candidate_files", "late", candidate_arguments()),
            ]
        ),
        config=AgentRunConfig(
            max_steps=4,
            max_tool_errors=1,
            require_plan_before_edit=False,
        ),
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert "frozen after the first successful write_file" in result.steps[-1].error_message


def test_missing_hypothesis_blocks_patch_submission(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_plan", "plan", plan_arguments()),
                response("submit_patch", "blocked"),
            ]
        ),
        max_tool_errors=1,
    )

    result = loop.run()

    assert result.stop_reason == "max_tool_errors"
    assert "submit_hypothesis" in result.steps[-1].error_message
    assert db.scalar(select(GeneratedPatch)) is None
    assert (
        db.scalar(select(AgentEvent).where(AgentEvent.event_type == "hypothesis_submitted")) is None
    )


def test_new_hypothesis_revises_previous_and_confirmed_one_stays_active(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_plan", "plan", plan_arguments()),
                response("submit_hypothesis", "first", hypothesis_arguments(confidence="low")),
                response(
                    "submit_hypothesis",
                    "second",
                    hypothesis_arguments(
                        summary="The inspected documentation confirms the no-op mock diagnosis.",
                        confidence="high",
                        status="confirmed",
                    ),
                ),
                response("submit_patch", "submit"),
            ]
        ),
        max_steps=5,
    )

    result = loop.run()

    assert result.stop_reason == "patch_submitted"
    from app.agents.hypotheses import active_run_hypothesis, run_hypotheses

    run = db.scalar(select(AgentRun))
    hypotheses = run_hypotheses(db, run.id)
    assert [(item.revision, item.status) for item in hypotheses] == [
        (1, "revised"),
        (2, "confirmed"),
    ]
    assert hypotheses[0].superseded_by_revision == 2
    assert active_run_hypothesis(db, run.id).revision == 2


@pytest.mark.parametrize(
    "arguments",
    [
        hypothesis_arguments(summary="x" * 2001),
        hypothesis_arguments(suspected_files=["../outside.py"]),
        hypothesis_arguments(suspected_files=[".gold/solution.py"]),
        hypothesis_arguments(gold_patch="PRIVATE-GOLD-HYPOTHESIS"),
        hypothesis_arguments(confidence="certain"),
        hypothesis_arguments(status="solved"),
    ],
)
def test_unsafe_hypothesis_is_rejected_without_logging_raw_input(db, workspace, arguments):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(responses=[response("submit_hypothesis", "unsafe", arguments)]),
        max_tool_errors=1,
    )

    result = loop.run()

    assert result.tool_errors == 1
    assert (
        db.scalar(select(AgentEvent).where(AgentEvent.event_type == "hypothesis_submitted")) is None
    )
    serialized = json.dumps([event.payload_json for event in db.scalars(select(AgentEvent))])
    for forbidden in ("PRIVATE-GOLD-HYPOTHESIS", "../outside.py", ".gold/solution.py", "x" * 2001):
        assert forbidden not in serialized


def test_hypothesis_secrets_are_redacted(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response(
                    "submit_hypothesis",
                    "hypothesis",
                    hypothesis_arguments(
                        summary="api_key=private-value",
                        supporting_evidence=["Bearer private-bearer"],
                    ),
                )
            ]
        ),
        max_steps=5,
    )

    result = loop.run()

    assert result.steps[0].success is True
    event = event_by_type(db, "hypothesis_submitted")
    serialized = json.dumps(event.payload_json)
    assert "private-value" not in serialized
    assert "private-bearer" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"file_path": "README.md", "content": "should not write"}),
        ("submit_patch", {}),
    ],
)
def test_edit_before_plan_is_rejected(db, workspace, name, args):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(responses=[response(name, "blocked", args)]),
        max_tool_errors=1,
    )
    result = loop.run()
    assert result.tool_errors == 1
    assert "submit_plan" in result.steps[0].error_message
    assert (workspace / "README.md").read_text() == "# Example\n"
    assert db.scalar(select(GeneratedPatch)) is None


def test_valid_plan_unlocks_edits_and_appears_in_detail_and_trace(db, workspace):
    calls = [
        response("write_file", "blocked", {"file_path": "README.md", "content": "blocked"}),
        response("read_file", "read", {"file_path": "README.md"}),
        response("submit_plan", "plan", plan_arguments()),
        response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
        response("submit_candidate_files", "candidates", candidate_arguments()),
        response("write_file", "write", {"file_path": "README.md", "content": "# Updated\n"}),
        response("submit_patch", "submit"),
    ]
    loop = create_loop(db, workspace, provider=MockModelProvider(responses=calls), max_steps=8)
    result = loop.run()
    assert result.stop_reason == "patch_submitted"
    assert result.tool_errors == 1
    assert (workspace / "README.md").read_text() == "# Updated\n"
    event = event_by_type(db, "plan_submitted")
    assert event.payload_json["accepted"] is True
    assert event.payload_json["revision"] == 1
    run = db.scalar(select(AgentRun))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        detail = client.get(f"/agent-runs/{run.id}")
        trace = client.get(f"/agent-runs/{run.id}/trace")
    assert detail.status_code == trace.status_code == 200
    assert detail.json()["latest_plan"]["plan"]["files_inspected"] == ["README.md"]
    assert trace.json()["latest_plan"] == detail.json()["latest_plan"]
    assert trace.json()["plan_status"] == "accepted"
    plan_event = next(e for e in trace.json()["events"] if e["event_type"] == "plan_submitted")
    assert plan_event["summary"] == "Plan revision 1 accepted"
    assert plan_event["severity"] == "info"
    assert plan_event["tool_name"] == "submit_plan"
    assert plan_event["file_paths"] == ["README.md", "src/calculator.py"]
    assert "hidden patch" not in detail.text + trace.text
    assert "gold/solution.py" not in detail.text + trace.text
    assert detail.json()["active_hypothesis"]["revision"] == 1
    assert detail.json()["hypotheses"][0]["status"] == "active"
    hypothesis_event = next(
        e for e in trace.json()["events"] if e["event_type"] == "hypothesis_submitted"
    )
    assert hypothesis_event["summary"] == "Hypothesis revision 1 active (medium confidence)"
    assert hypothesis_event["tool_name"] == "submit_hypothesis"
    assert hypothesis_event["file_paths"] == ["README.md"]
    assert detail.json()["candidate_files"] == [
        {
            "path": "README.md",
            "reason": "Evidence indicates README.md is relevant to the reported behavior.",
            "confidence": "medium",
        }
    ]
    candidate_event = next(
        e for e in trace.json()["events"] if e["event_type"] == "candidate_files_submitted"
    )
    assert candidate_event["summary"] == "Submitted 1 ranked candidate file"
    assert candidate_event["file_paths"] == ["README.md"]


@pytest.mark.parametrize("files", [[], ["README.md"], ["src/calculator.py"]])
def test_plan_cannot_claim_unobserved_evidence(db, workspace, files):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("list_files", "list"),
                response("submit_plan", "plan", plan_arguments(files_inspected=files)),
                response("submit_patch", "submit"),
            ]
        ),
        max_steps=3,
    )
    result = loop.run()
    assert not result.steps[1].success
    assert not result.steps[2].success
    assert event_by_type(db, "plan_submitted").payload_json["accepted"] is False
    assert db.scalar(select(GeneratedPatch)) is None


def test_rejected_plan_can_be_revised_after_inspection(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("submit_plan", "missing", plan_arguments()),
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_plan", "revised", plan_arguments()),
                response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
                response("submit_patch", "submit"),
            ]
        ),
        max_steps=5,
    )
    result = loop.run()
    assert result.stop_reason == "patch_submitted"
    events = list(
        db.scalars(
            select(AgentEvent)
            .where(AgentEvent.event_type == "plan_submitted")
            .order_by(AgentEvent.created_at)
        )
    )
    assert [(e.payload_json["revision"], e.payload_json["accepted"]) for e in events] == [
        (1, False),
        (2, True),
    ]


def test_plan_revision_budget_cannot_be_bypassed(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("submit_plan", "first", plan_arguments()),
                response("submit_plan", "revision", plan_arguments()),
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_plan", "over-limit", plan_arguments()),
                response("submit_patch", "submit"),
            ]
        ),
        config=AgentRunConfig(max_steps=5, max_tool_errors=5, max_plan_revisions=1),
    )
    result = loop.run()
    assert "revision limit" in result.steps[3].error_message
    assert result.steps[4].success is False
    assert db.scalar(select(GeneratedPatch)) is None


@pytest.mark.parametrize(
    "args",
    [
        plan_arguments(issue_summary="x" * 2001),
        plan_arguments(files_inspected=["../outside.py"]),
        plan_arguments(files_likely_to_modify=[".gold/solution.py"]),
        plan_arguments(gold_patch="PRIVATE-GOLD-TEXT"),
        {"issue_summary": "missing the other fields"},
    ],
)
def test_unsafe_or_malformed_plan_is_rejected_without_logging_raw_input(db, workspace, args):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(responses=[response("submit_plan", "plan", args)]),
        max_tool_errors=1,
    )
    result = loop.run()
    assert result.tool_errors == 1
    event = event_by_type(db, "plan_submitted")
    assert event.payload_json["status"] == "rejected"
    assert event.payload_json["plan"] is None
    serialized = json.dumps([e.payload_json for e in db.scalars(select(AgentEvent))])
    for forbidden in ("PRIVATE-GOLD-TEXT", "../outside.py", ".gold/solution.py", "x" * 2001):
        assert forbidden not in serialized


def test_plan_secrets_are_redacted_and_duplicate_evidence_does_not_count_twice(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response(
                    "submit_plan",
                    "plan",
                    plan_arguments(
                        files_inspected=["README.md", "./README.md"],
                        risk_rollback_notes=(
                            "Never use api_key=private-secret or Bearer private-bearer "
                            "or OPENAI_API_KEY=private-env-value"
                        ),
                    ),
                ),
            ]
        ),
        config=AgentRunConfig(max_steps=2, plan_min_evidence_files=2),
    )
    result = loop.run()
    assert "at least 2" in result.steps[1].error_message
    serialized = json.dumps([e.payload_json for e in db.scalars(select(AgentEvent))])
    assert "private-secret" not in serialized
    assert "private-bearer" not in serialized
    assert "private-env-value" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"file_path": "README.md"}),
        ("search_code", {"query": "Example"}),
    ],
)
def test_successful_observations_ground_a_plan(db, workspace, tool, args):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response(tool, "inspect", args),
                response("submit_plan", "plan", plan_arguments()),
                response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
                response("submit_patch", "submit"),
            ]
        ),
        max_steps=5,
    )
    assert loop.run().stop_reason == "patch_submitted"


def test_retrieved_files_can_ground_plan(db, workspace, monkeypatch):
    monkeypatch.setattr(
        "app.repository_indexing.RepositoryRetrievalService.retrieve",
        Mock(
            return_value=RelevantFilesResult(
                query="Example",
                files=[
                    RelevantFile(
                        file_path="README.md",
                        language="Markdown",
                        file_kind="docs",
                        matched_symbols=[],
                        matched_snippets=[],
                        relevance_score=1.0,
                        size_bytes=10,
                    )
                ],
            )
        ),
    )
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("retrieve_relevant_files", "retrieve", {"query": "Example"}),
                response("submit_candidate_files", "candidates", candidate_arguments()),
                response("submit_plan", "plan", plan_arguments()),
                response("submit_hypothesis", "hypothesis", hypothesis_arguments()),
                response("submit_patch", "submit"),
            ]
        ),
        max_steps=5,
    )
    assert loop.run().stop_reason == "patch_submitted"
    assert (
        event_by_type(db, "candidate_files_submitted").payload_json["ranked_files"][0]["path"]
        == "README.md"
    )


def test_plan_total_size_is_bounded(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response(
                    "submit_plan",
                    "large",
                    plan_arguments(
                        files_inspected=["a" * 200] * 50,
                        files_likely_to_modify=["b" * 200] * 50,
                    ),
                ),
            ]
        ),
        max_tool_errors=1,
    )
    assert "16384-byte limit" in loop.run().steps[0].error_message
    assert event_by_type(db, "plan_submitted").payload_json["plan"] is None


def test_rejected_revision_closes_previously_accepted_gate(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("read_file", "read", {"file_path": "README.md"}),
                response("submit_plan", "accepted", plan_arguments()),
                response("submit_plan", "rejected", plan_arguments(files_inspected=[])),
                response("write_file", "blocked", {"file_path": "README.md", "content": "blocked"}),
            ]
        ),
    )
    result = loop.run()
    assert result.steps[1].success is True
    assert result.steps[2].success is False
    assert result.steps[3].success is False
    assert (workspace / "README.md").read_text() == "# Example\n"
    from app.run_traces.service import AgentRunTraceService

    trace = AgentRunTraceService(db).get(db.scalar(select(AgentRun)).id)
    assert trace.plan_status == "rejected"
    assert trace.latest_plan.revision == 2
    rejected = [event for event in trace.events if event.event_type == "plan_submitted"][-1]
    assert rejected.severity == "warning"


def test_same_response_tool_batch_cannot_skip_planning(db, workspace):
    calls = [
        ModelToolCall(
            id="early",
            name="write_file",
            arguments={"file_path": "README.md", "content": "blocked"},
        ),
        ModelToolCall(id="read", name="read_file", arguments={"file_path": "README.md"}),
        ModelToolCall(id="plan", name="submit_plan", arguments=plan_arguments()),
        ModelToolCall(
            id="candidates",
            name="submit_candidate_files",
            arguments=candidate_arguments(),
        ),
        ModelToolCall(
            id="write",
            name="write_file",
            arguments={"file_path": "README.md", "content": "# Changed\n"},
        ),
        ModelToolCall(id="submit", name="submit_patch"),
    ]
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(tool_calls=calls),
        max_steps=6,
        config=AgentRunConfig(max_steps=6, require_hypothesis_before_patch=False),
    )
    result = loop.run()
    assert result.stop_reason == "patch_submitted"
    assert [step.success for step in result.steps] == [False, True, True, True, True, True]
    assert result.model_calls == 1 and result.tool_errors == 1


def test_explicit_planning_opt_out_supports_legacy_flows(db, workspace):
    loop = create_loop(
        db,
        workspace,
        provider=MockModelProvider(
            responses=[
                response("submit_patch", "submit"),
            ]
        ),
        config=AgentRunConfig(
            require_plan_before_edit=False,
            require_hypothesis_before_patch=False,
            require_candidate_files_before_edit=False,
        ),
    )
    assert loop.run().stop_reason == "patch_submitted"
    assert db.scalar(select(AgentEvent).where(AgentEvent.event_type == "plan_submitted")) is None


@pytest.mark.parametrize(
    "config",
    [
        {"max_plan_revisions": -1},
        {"max_plan_revisions": 11},
        {"max_plan_revisions": True},
        {"plan_min_evidence_files": 0},
        {"plan_min_evidence_files": 51},
        {"max_candidate_files": 0},
        {"max_candidate_files": 51},
    ],
)
def test_invalid_planning_config_rejected(config):
    with pytest.raises(ValidationError):
        AgentRunConfig(**config)


def test_prompt_includes_planning_constraints_without_gold(db, workspace):
    loop = create_loop(
        db, workspace, config=AgentRunConfig(max_plan_revisions=1, plan_min_evidence_files=2)
    )
    prompt = json.dumps(loop.prompt_preview())
    assert "submit_plan" in prompt
    assert "Minimum distinct evidence files per plan: 2" in prompt
    assert "Maximum plan revisions after the initial submission: 1" in prompt
    assert "submit_candidate_files" in prompt
    assert "Maximum ranked candidate files: 10" in prompt
    assert "hidden patch" not in prompt
    assert "gold/solution.py" not in prompt


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
