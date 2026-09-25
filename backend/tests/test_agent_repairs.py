import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.orchestrator import PreparedWorkspace
from app.agents.repairs import MAX_FAILURE_FEEDBACK_BYTES, safe_failure_summary
from app.api.routes.agent_run_orchestration import (
    get_model_provider_factory,
    get_workspace_preparer,
)
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.model_providers import MockModelProvider, ModelProviderResponse, ModelToolCall
from app.models import AgentEvent, AgentRun, BenchmarkTask, GoldPatch, Repository
from app.patches import PatchApplyError, PatchService

BAD = "def add(a, b):\n    return a - b\n"
WRONG = "def add(a, b):\n    return 0\n"
GOOD = "def add(a, b):\n    return a + b\n"


def call(name, **arguments):
    return ModelProviderResponse(
        content="", tool_calls=[ModelToolCall(id=f"call-{name}", name=name, arguments=arguments)]
    )


def candidate(source):
    return [call("write_file", file_path="calculator.py", content=source), call("submit_patch")]


class ObservingMock(MockModelProvider):
    def __init__(self, responses):
        super().__init__(responses=responses)
        self.conversations = []

    def generate_response(self, messages, tools=None):
        self.conversations.append([message.content for message in messages])
        return super().generate_response(messages, tools)


@pytest.fixture()
def harness(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calculator.py").write_text(BAD, encoding="utf-8")
    (workspace / "notes.txt").write_text("baseline\n", encoding="utf-8")
    for args in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["config", "core.autocrlf", "false"],
        ["add", "."],
        ["commit", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(
            name="calculator", owner="example", url="https://github.com/example/calculator"
        )
        db.add(repo)
        db.flush()
        task = BenchmarkTask(
            repository_id=repo.id,
            issue_number=1,
            issue_title="Fix addition",
            issue_body="Addition is broken",
            base_commit="base",
            fix_commit="HIDDEN_FIX_COMMIT",
            status="ready",
            setup_commands=[],
            test_commands=[
                f'"{sys.executable}" -B -c "from calculator import add; assert add(2, 3) == 5"'
            ],
        )
        db.add(task)
        db.flush()
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                patch_text="HIDDEN_GOLD_PATCH",
                changed_files=["calculator.py", "HIDDEN_GOLD_FILE.py"],
                test_files=[],
            )
        )
        db.commit()
        state = SimpleNamespace(db=db, task=task, workspace=workspace, closed=False)

        def prepare(**kwargs):
            return PreparedWorkspace(
                workspace_id="test-repair",
                path=workspace,
                cleanup=lambda: setattr(state, "closed", True),
            )

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_workspace_preparer] = lambda: SimpleNamespace(prepare=prepare)
        with TestClient(app) as client:

            def start(responses, **config):
                state.provider = ObservingMock(responses)
                app.dependency_overrides[get_model_provider_factory] = lambda: SimpleNamespace(
                    create=lambda *args, **kwargs: state.provider
                )
                response = client.post(
                    f"/agent-runs/{task.id}/start",
                    json={
                        "model_provider": "mock",
                        "max_steps": 10,
                        "max_repair_attempts": 1,
                        "enable_test_tool": False,
                        # Legacy repair cases isolate patch budgets from planning steps.
                        "require_plan_before_edit": False,
                        "require_hypothesis_before_patch": False,
                        "require_candidate_files_before_edit": False,
                        **config,
                    },
                )
                assert response.status_code == 200, response.text
                state.payload = response.json()
                state.run = db.get(AgentRun, UUID(state.payload["id"]))
                return state.payload

            state.start = start
            state.client = client
            yield state
    engine.dispose()


def event_payloads(h, kind):
    return [
        event.payload_json for event in h.db.scalars(select(AgentEvent)) if event.event_type == kind
    ]


def test_accepted_plan_remains_in_effect_across_patch_repairs(harness):
    h = harness
    result = h.start(
        [
            call("read_file", file_path="calculator.py"),
            call(
                "submit_plan",
                issue_summary="Repair addition",
                suspected_root_cause="Addition currently subtracts",
                files_inspected=["calculator.py"],
                files_likely_to_modify=["calculator.py"],
                test_strategy="Run the configured addition test",
                risk_rollback_notes="Revert the one-line change if necessary",
            ),
            call(
                "submit_hypothesis",
                summary="Addition subtracts instead of adding",
                suspected_files=["calculator.py"],
                supporting_evidence=["The inspected implementation returns a - b"],
                confidence="high",
                status="active",
            ),
            *candidate(WRONG),
            call(
                "submit_hypothesis",
                summary="The first correction was incomplete and still fails the configured test",
                suspected_files=["calculator.py"],
                supporting_evidence=["Post-patch test feedback rejected the first candidate"],
                confidence="high",
                status="confirmed",
            ),
            *candidate(GOOD),
        ],
        require_plan_before_edit=True,
        require_hypothesis_before_patch=True,
    )
    assert result["status"] == "completed"
    assert result["repair_attempts_used"] == 1
    assert len(event_payloads(h, "plan_submitted")) == 1
    assert event_payloads(h, "plan_submitted")[0]["accepted"] is True
    hypotheses = event_payloads(h, "hypothesis_submitted")
    assert [(item["revision"], item["status"]) for item in hypotheses] == [
        (1, "revised"),
        (2, "confirmed"),
    ]
    assert h.run.evaluation_metric.tests_passed is True


def test_first_patch_passes_without_retry(harness):
    h = harness
    result = h.start(candidate(GOOD) + candidate(WRONG))
    assert result["status"] == "completed"
    assert result["repair_attempts_used"] == 0
    assert result["final_patch_passed_tests"] is True and result["failure_summary"] is None
    assert result["final_patch_id"] == result["generated_patch_id"]
    assert len(h.provider.conversations) == 2
    assert len(h.run.generated_patches) == 1
    assert h.closed


def test_second_patch_passes_and_metrics_use_final_candidate(harness):
    h = harness
    result = h.start(candidate(WRONG) + candidate(GOOD))
    assert result["status"] == "completed", result
    assert result["repair_attempts_used"] == 1 and result["final_patch_passed_tests"] is True
    first, second = h.run.generated_patches
    assert first.version == 1 and second.version == 2 and first.id != second.id
    assert "return 0" in first.patch_text and "return a + b" in second.patch_text
    assert result["final_patch_id"] == str(second.id)
    assert not first.is_selected and second.is_selected
    tests = [test for test in h.run.test_results if test.phase == "post_patch"]
    assert [(test.attempt_number, test.generated_patch_id, test.passed) for test in tests] == [
        (1, first.id, False),
        (2, second.id, True),
    ]
    assert h.run.evaluation_metric.tests_passed is True
    assert h.run.evaluation_metric.patch_applied is True
    assert h.run.evaluation_metric.modified_files_count == 1
    assert "Post-patch test phase failed" in json.dumps(h.provider.conversations[2])
    patch = h.client.get(f"/agent-runs/{h.run.id}/patch").json()
    assert patch["id"] == str(second.id) and patch["version"] == 2
    assert h.client.get(f"/agent-runs/{h.run.id}/patches").json()[0]["version"] == 1
    detail = h.client.get(f"/agent-runs/{h.run.id}").json()
    assert detail["final_patch_id"] == str(second.id) and detail["repair_attempts_used"] == 1
    api_tests = h.client.get(f"/agent-runs/{h.run.id}/tests").json()
    assert api_tests[-1]["attempt_number"] == 2
    all_data = json.dumps(result) + json.dumps(h.provider.conversations) + json.dumps(detail)
    for hidden in ("HIDDEN_GOLD_PATCH", "HIDDEN_GOLD_FILE", "HIDDEN_FIX_COMMIT"):
        assert hidden not in all_data
    assert len(event_payloads(h, "repair_attempt_started")) == 2
    assert event_payloads(h, "final_patch_selected")[-1]["generated_patch_id"] == str(second.id)
    assert h.closed


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_attempts_exhausted_and_limit_is_enforced(harness, limit):
    h = harness
    result = h.start(candidate(WRONG) * (limit + 2), max_repair_attempts=limit)
    assert result["status"] == "failed"
    assert result["repair_attempts_used"] == limit
    assert result["final_patch_passed_tests"] is False
    assert "Post-patch test phase failed" in result["failure_summary"]
    assert len(h.run.generated_patches) == limit + 1
    assert len(h.provider.conversations) == (limit + 1) * 2
    assert h.run.evaluation_metric.tests_passed is False
    assert h.run.evaluation_metric.patch_applied is True
    assert event_payloads(h, "repair_limit_reached")
    assert result["failure_category"] == (
        "max_repair_attempts_reached" if limit else "post_patch_tests_failed"
    )
    assert h.closed


def test_invalid_workspace_patch_is_repairable_with_original_limits(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(settings, "patch_max_bytes", 600)
    result = h.start(candidate("x = '" + "a" * 1000 + "'\n") + candidate(GOOD))
    assert result["status"] == "completed", result
    assert result["repair_attempts_used"] == 1
    assert len(h.run.generated_patches) == 1
    outcomes = event_payloads(h, "repair_attempt_completed")
    assert outcomes[0]["outcome"] == "invalid_patch" and outcomes[0]["generated_patch_id"] is None
    assert outcomes[1]["outcome"] == "passed"
    assert "maximum size" in json.dumps(h.provider.conversations[2])


def test_invalid_application_retries_and_keeps_version_without_test_results(harness, monkeypatch):
    h = harness
    original = PatchService.ensure_patch_applied
    calls = 0

    def fail_once(service, patch_text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PatchApplyError("Patch does not apply cleanly.")
        return original(service, patch_text)

    monkeypatch.setattr(PatchService, "ensure_patch_applied", fail_once)
    result = h.start(candidate(WRONG) + candidate(GOOD))
    assert result["status"] == "completed"
    assert len(h.run.generated_patches) == 2
    assert h.run.generated_patch.version == 2
    assert [test.attempt_number for test in h.run.test_results if test.phase == "post_patch"] == [2]
    assert h.run.evaluation_metric.tests_passed is True


def test_patch_apply_failure_is_classified(harness, monkeypatch):
    def fail_apply(service, patch_text):
        raise PatchApplyError("Patch does not apply cleanly.")

    monkeypatch.setattr(PatchService, "ensure_patch_applied", fail_apply)
    result = harness.start(candidate(GOOD), max_repair_attempts=0)

    assert result["status"] == "failed"
    assert result["failure_category"] == "patch_apply_failed"
    assert harness.run.failure.category == "patch_apply_failed"


def test_patch_quality_block_is_classified(harness, monkeypatch):
    monkeypatch.setattr(settings, "max_patch_changed_lines", 1)

    result = harness.start(candidate(GOOD), max_repair_attempts=0)

    assert result["status"] == "failed"
    assert result["failure_category"] == "patch_quality_blocked"
    assert harness.run.failure.category == "patch_quality_blocked"


def test_max_tool_errors_is_not_reset_by_repair(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(settings, "patch_max_bytes", 600)
    result = h.start(candidate("x = '" + "a" * 1000 + "'\n") + candidate(GOOD), max_tool_errors=1)
    assert result["status"] == "failed"
    assert result["repair_attempts_used"] == 0
    assert "tool error limit" in result["failure_summary"]
    assert len(h.provider.conversations) == 2


def test_max_steps_is_shared_across_repairs(harness):
    h = harness
    result = h.start(candidate(WRONG) + candidate(GOOD), max_steps=3)
    assert result["status"] == "failed"
    assert len(h.provider.conversations) == 3
    assert "Maximum step limit" in result["failure_summary"]
    assert len(h.run.generated_patches) == 1
    assert (h.workspace / "calculator.py").read_text() == WRONG
    assert h.run.evaluation_metric.tests_passed is False
    assert result["failure_category"] == "max_steps_reached"


def test_keep_earlier_passing_patch_if_later_attempt_fails(harness):
    h = harness
    result = h.start(candidate(GOOD) + candidate(WRONG), stop_on_first_passing_patch=False)
    assert result["status"] == "completed", result
    assert result["repair_attempts_used"] == 1
    assert result["final_patch_id"] == str(h.run.generated_patches[0].id)
    assert (h.workspace / "calculator.py").read_text() == GOOD
    assert h.run.evaluation_metric.tests_passed is True
    assert h.client.get(f"/agent-runs/{h.run.id}/patch").json()["version"] == 1


def test_disabled_post_patch_tests_do_not_claim_passing(harness):
    h = harness
    result = h.start(candidate(GOOD) + candidate(WRONG), run_tests_after_patch=False)
    assert result["status"] == "completed"
    assert result["final_patch_passed_tests"] is None
    assert all(test.phase != "post_patch" for test in h.run.test_results)
    assert h.run.evaluation_metric.tests_passed is False
    assert h.run.evaluation_metric.patch_applied is True
    assert len(h.provider.conversations) == 2


def test_metrics_exclude_abandoned_files_and_diagnostic_test_failures(harness):
    h = harness
    result = h.start(
        [
            call("run_tests", command=h.task.test_commands[0]),
            call("write_file", file_path="notes.txt", content="unrelated edit\n"),
            *candidate(WRONG),
            call("write_file", file_path="notes.txt", content="baseline\n"),
            *candidate(GOOD),
        ],
        enable_test_tool=True,
    )
    assert result["status"] == "completed", result
    assert h.run.generated_patches[0].changed_files == ["calculator.py", "notes.txt"]
    assert h.run.generated_patch.changed_files == ["calculator.py"]
    metric = h.run.evaluation_metric
    assert metric.tests_passed and metric.patch_applied
    assert metric.modified_files_count == 1 and metric.unrelated_files_count == 0
    assert any(test.generated_patch_id is None and not test.passed for test in h.run.test_results)


def test_submission_skips_stale_followup_calls_before_repair(harness):
    h = harness
    stale = ModelProviderResponse(
        content="",
        tool_calls=[
            ModelToolCall(id="submit-first", name="submit_patch"),
            ModelToolCall(
                id="stale-write",
                name="write_file",
                arguments={
                    "file_path": "notes.txt",
                    "content": "must not execute",
                },
            ),
        ],
    )
    result = h.start(
        [call("write_file", file_path="calculator.py", content=WRONG), stale, *candidate(GOOD)]
    )
    assert result["status"] == "completed"
    assert (h.workspace / "notes.txt").read_text() == "baseline\n"
    assert "Not executed: patch submission ended" in json.dumps(h.provider.conversations[2])


def test_review_does_not_transfer_between_patch_versions(harness):
    h = harness
    h.start(candidate(WRONG) + candidate(GOOD))
    first, final = h.run.generated_patches
    from app.models import HumanReview

    h.db.add(HumanReview(generated_patch_id=first.id, decision="approved", reviewer_name="Human"))
    h.db.commit()
    assert first.review_status == "approved" and final.review_status == "pending"
    assert h.client.get(f"/agent-runs/{h.run.id}/patch").json()["review_status"] == "pending"


def test_unrecoverable_test_error_stops_and_retains_previous_passing_candidate(
    harness, monkeypatch
):
    from app.test_execution import TestExecutionService

    original = TestExecutionService.run_post_patch_tests
    attempts = 0

    def crash_after_first(service, **options):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("test executor unavailable; api_key=private-error")
        return original(service, **options)

    monkeypatch.setattr(TestExecutionService, "run_post_patch_tests", crash_after_first)
    h = harness
    result = h.start(candidate(GOOD) + candidate(WRONG), stop_on_first_passing_patch=False)
    assert result["status"] == "failed"
    assert result["final_patch_id"] == str(h.run.generated_patches[0].id)
    assert "private-error" not in json.dumps(result)
    assert (h.workspace / "calculator.py").read_text() == GOOD
    assert h.closed


@pytest.mark.parametrize("include_output", [True, False])
def test_failure_feedback_is_redacted_bounded_and_optional(harness, include_output):
    h = harness
    h.task.test_commands = [
        (
            f"\"{sys.executable}\" -B -c \"print('api_key=private-value'); print('OUTPUT_MARKER'); "
            "print('z' * 8000); raise SystemExit(1)\""
        )
    ]
    h.db.commit()
    result = h.start(
        candidate(WRONG) + candidate(GOOD), include_test_failure_feedback=include_output
    )
    summaries = [item["failure_summary"] for item in event_payloads(h, "repair_attempt_completed")]
    for summary in summaries:
        assert len(summary.encode()) <= MAX_FAILURE_FEEDBACK_BYTES
        assert "private-value" not in summary
        assert ("OUTPUT_MARKER" in summary) == include_output
        if include_output:
            assert "truncated" in summary and "[REDACTED]" in summary
    assert "private-value" not in json.dumps(h.provider.conversations)
    assert "private-value" not in json.dumps(result)


def test_feedback_truncates_after_redaction_at_utf8_boundary():
    summary = safe_failure_summary("secret=" + "x" * 9000 + "\n" + "\u00e9" * 5000)
    assert len(summary.encode()) <= MAX_FAILURE_FEEDBACK_BYTES
    assert "[REDACTED]" in summary and "xxxx" not in summary


@pytest.mark.parametrize("limit", [-1, 6, 1.5, True])
def test_invalid_repair_config_rejected_before_run_creation(harness, limit):
    h = harness
    response = h.client.post(f"/agent-runs/{h.task.id}/start", json={"max_repair_attempts": limit})
    assert response.status_code == 422
    assert h.db.scalar(select(AgentRun)) is None
