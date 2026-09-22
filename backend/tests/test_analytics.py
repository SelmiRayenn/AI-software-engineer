from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkPack,
    BenchmarkPackRun,
    BenchmarkPackRunTask,
    BenchmarkTask,
    EvaluationMetric,
    GeneratedPatch,
    GoldPatch,
    HumanReview,
    Repository,
)

engine = create_engine(
    "sqlite://",
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


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def create_task(db: Session, owner: str, name: str) -> BenchmarkTask:
    repository = Repository(
        owner=owner,
        name=name,
        url=f"https://github.com/{owner}/{name}",
        default_branch="main",
        language="Python",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=1,
        issue_title=f"Fix {name}",
        issue_body="A reproducible issue",
        base_commit="a" * 40,
        setup_commands=["python -m pip install -e ."],
        test_commands=["pytest"],
        status="ready",
    )
    db.add(task)
    db.flush()
    return task


def create_run(
    db: Session,
    task: BenchmarkTask,
    *,
    status: str = "completed",
    provider: str = "mock",
    model: str = "mock-model",
    started_at: datetime | None = None,
    metric_values: dict | None = None,
    review: str | None = None,
) -> AgentRun:
    started_at = started_at or datetime(2026, 1, 15, tzinfo=UTC)
    run = AgentRun(
        benchmark_task=task,
        model_provider=provider,
        model_name=model,
        status=status,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=30) if status != "running" else None,
    )
    db.add(run)
    db.flush()

    if metric_values is not None:
        defaults = {
            "file_localization_score": 0.0,
            "patch_applied": False,
            "tests_passed": False,
            "baseline_tests_passed": True,
            "post_patch_tests_passed": False,
            "hidden_tests_passed": None,
            "hidden_tests_run_count": 0,
            "hidden_tests_failed_count": 0,
            "issue_resolved": False,
            "regression_detected": False,
            "issue_specific_score": 0.0,
            "modified_files_count": 0,
            "unrelated_files_count": 0,
            "tokens_used": 0,
            "estimated_cost": 0.0,
            "execution_time_seconds": 0.0,
        }
        defaults.update(metric_values)
        db.add(EvaluationMetric(agent_run_id=run.id, **defaults))

    if review is not None:
        patch = GeneratedPatch(
            agent_run_id=run.id,
            patch_text="diff --git a/app.py b/app.py\n",
            changed_files=["app.py"],
            version=1,
            is_selected=True,
        )
        db.add(patch)
        db.flush()
        db.add(
            HumanReview(
                generated_patch_id=patch.id,
                decision=review,
                reviewer_name="Reviewer",
            )
        )

    db.flush()
    return run


def attach_run_to_pack(
    db: Session,
    run: AgentRun,
    *,
    slug: str,
    version: str = "1.0",
) -> BenchmarkPack:
    pack = BenchmarkPack(name=slug.title(), slug=slug, version=version)
    db.add(pack)
    db.flush()
    pack_run = BenchmarkPackRun(
        benchmark_pack_id=pack.id,
        pack_name=pack.name,
        pack_slug=pack.slug,
        pack_version=pack.version,
        model_provider=run.model_provider,
        model_name=run.model_name,
        run_config={},
        include_hidden_tests=False,
        stop_on_task_failure=False,
        status="completed",
        started_at=run.started_at,
        completed_at=run.completed_at,
        aggregates={},
    )
    db.add(pack_run)
    db.flush()
    db.add(
        BenchmarkPackRunTask(
            benchmark_pack_run_id=pack_run.id,
            benchmark_task_id=run.benchmark_task_id,
            agent_run_id=run.id,
            order_index=0,
            task_snapshot={},
            definition_hash="b" * 64,
            status=run.status,
            metric_summary={},
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
    )
    db.flush()
    return pack


def add_event(db: Session, run: AgentRun, event_type: str, payload: dict) -> None:
    db.add(
        AgentEvent(
            agent_run_id=run.id,
            event_type=event_type,
            payload_json=payload,
        )
    )


def add_gold_patch(db: Session, task: BenchmarkTask, changed_files: list[str]) -> None:
    db.add(
        GoldPatch(
            benchmark_task_id=task.id,
            changed_files=changed_files,
            patch_text="trusted gold patch",
            test_files=[],
        )
    )


def add_generated_patch(db: Session, run: AgentRun, changed_files: list[str]) -> None:
    db.add(
        GeneratedPatch(
            agent_run_id=run.id,
            patch_text="generated patch",
            changed_files=changed_files,
            version=1,
            is_selected=True,
        )
    )


def add_inspections(
    db: Session,
    run: AgentRun,
    file_paths: list[str],
    *,
    start: datetime | None = None,
) -> None:
    started = start or datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    for index, file_path in enumerate(file_paths):
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_tool_call",
                payload_json={
                    "tool_name": "read_file",
                    "success": True,
                    "files_read": [file_path],
                },
                created_at=started + timedelta(seconds=index),
            )
        )


def test_empty_analytics_has_defined_zero_state(client: TestClient) -> None:
    response = client.get("/analytics/summary")

    assert response.status_code == 200
    assert response.json() == {
        "total_runs": 0,
        "completed_runs": 0,
        "failed_runs": 0,
        "approved_patches": 0,
        "rejected_patches": 0,
        "patch_apply_rate": 0.0,
        "visible_test_pass_rate": 0.0,
        "hidden_test_pass_rate": None,
        "issue_resolved_rate": 0.0,
        "regression_rate": 0.0,
        "average_file_localization_score": 0.0,
        "average_issue_specific_score": 0.0,
        "average_modified_files_count": 0.0,
        "average_unrelated_files_count": 0.0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "average_cost_per_run": 0.0,
        "average_execution_time_seconds": 0.0,
    }
    assert client.get("/analytics/by-repository").json() == []
    assert client.get("/analytics/by-pack").json() == []


def test_empty_tool_usage_has_defined_zero_state(client: TestClient) -> None:
    response = client.get("/analytics/tool-usage")

    assert response.status_code == 200
    assert response.json() == {
        "total_tool_calls": 0,
        "successful_tool_calls": 0,
        "failed_tool_calls": 0,
        "unknown_tool_calls": 0,
        "malformed_tool_calls": 0,
        "tool_error_rate": 0.0,
        "average_tool_calls_per_run": 0.0,
        "most_used_tools": [],
        "most_failed_tools": [],
        "tool_error_counts_by_type": {},
        "runs_with_tool_errors": 0,
        "tool_errors_by_model": [],
    }


def test_tool_usage_counts_modern_calls_once_and_classifies_errors(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "tools")
        run = create_run(db, task, provider="openai", model="agent-model", metric_values={})
        add_event(
            db,
            run,
            "tool_call_requested",
            {"tool_call": {"id": "call-1", "name": "read_file", "arguments": {}}},
        )
        add_event(
            db,
            run,
            "agent_tool_call",
            {"tool_name": "read_file", "success": True},
        )
        add_event(
            db,
            run,
            "tool_call_completed",
            {"tool_call_id": "call-1", "tool_name": "read_file"},
        )
        add_event(
            db,
            run,
            "tool_call_requested",
            {"tool_call": {"id": "call-2", "name": "delete_repository", "arguments": {}}},
        )
        add_event(
            db,
            run,
            "tool_call_failed",
            {
                "tool_call_id": "call-2",
                "tool_name": "delete_repository",
                "failure_category": "unknown_tool",
                "error_message": "Unknown tool",
            },
        )
        add_event(
            db,
            run,
            "tool_call_requested",
            {"tool_call": {"id": "call-3", "arguments": "not-an-object"}},
        )
        add_event(
            db,
            run,
            "tool_call_failed",
            {
                "tool_call_id": "call-3",
                "tool_name": "malformed_tool_call",
                "failure_category": "malformed_tool_call",
            },
        )
        db.commit()

    payload = client.get("/analytics/tool-usage").json()

    assert payload["total_tool_calls"] == 3
    assert payload["successful_tool_calls"] == 1
    assert payload["failed_tool_calls"] == 2
    assert payload["unknown_tool_calls"] == 1
    assert payload["malformed_tool_calls"] == 1
    assert payload["tool_error_rate"] == pytest.approx(2 / 3)
    assert payload["average_tool_calls_per_run"] == pytest.approx(3.0)
    assert payload["runs_with_tool_errors"] == 1
    assert payload["most_used_tools"] == [
        {"tool_name": "delete_repository", "call_count": 1},
        {"tool_name": "malformed_tool_call", "call_count": 1},
        {"tool_name": "read_file", "call_count": 1},
    ]
    assert payload["most_failed_tools"] == [
        {"tool_name": "delete_repository", "failed_count": 1},
        {"tool_name": "malformed_tool_call", "failed_count": 1},
    ]
    assert payload["tool_error_counts_by_type"] == {
        "malformed_tool_call": 1,
        "unknown_tool": 1,
    }
    assert payload["tool_errors_by_model"] == [
        {
            "model_provider": "openai",
            "model_name": "agent-model",
            "total_tool_calls": 3,
            "failed_tool_calls": 2,
            "unknown_tool_calls": 1,
            "malformed_tool_calls": 1,
            "tool_error_rate": pytest.approx(2 / 3),
            "runs_with_tool_errors": 1,
        }
    ]


def test_tool_usage_supports_legacy_events_and_all_filters(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        included_task = create_task(db, "acme", "included-tools")
        excluded_task = create_task(db, "other", "excluded-tools")
        included = create_run(
            db,
            included_task,
            provider="mock",
            model="included-model",
            started_at=datetime(2026, 2, 10, tzinfo=UTC),
            metric_values={},
        )
        excluded = create_run(
            db,
            excluded_task,
            provider="local",
            model="excluded-model",
            started_at=datetime(2026, 3, 10, tzinfo=UTC),
            metric_values={},
        )
        add_event(
            db,
            included,
            "agent_tool_call",
            {"tool_name": "list_files", "success": True},
        )
        add_event(
            db,
            included,
            "agent_tool_call",
            {
                "tool_name": "run_tests",
                "success": False,
                "error_message": "Command timed out",
            },
        )
        add_event(
            db,
            excluded,
            "agent_tool_call",
            {"tool_name": "read_file", "success": True},
        )
        pack = attach_run_to_pack(db, included, slug="tool-pack")
        db.commit()
        filters = {
            "benchmark_pack_id": str(pack.id),
            "repository_id": str(included_task.repository_id),
            "model_provider": "mock",
            "model_name": "included-model",
            "date_from": "2026-02-01T00:00:00Z",
            "date_to": "2026-02-28T23:59:59Z",
        }

    payload = client.get("/analytics/tool-usage", params=filters).json()

    assert payload["total_tool_calls"] == 2
    assert payload["successful_tool_calls"] == 1
    assert payload["failed_tool_calls"] == 1
    assert payload["tool_error_counts_by_type"] == {"timeout": 1}
    assert payload["tool_errors_by_model"][0]["model_name"] == "included-model"

    for key, value in (
        ("benchmark_pack_id", str(pack.id)),
        ("repository_id", str(included_task.repository_id)),
        ("model_provider", "mock"),
        ("model_name", "included-model"),
    ):
        assert client.get("/analytics/tool-usage", params={key: value}).json()[
            "total_tool_calls"
        ] == 2

    assert client.get(
        "/analytics/tool-usage", params={"model_provider": "anthropic"}
    ).json()["total_tool_calls"] == 0


def test_file_localization_top_k_uses_first_unique_inspection_order(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "ranking")
        add_gold_patch(db, task, ["src/target.py"])
        first = create_run(db, task, model="ranked", metric_values={})
        third = create_run(db, task, model="ranked", metric_values={})
        fifth = create_run(db, task, model="ranked", metric_values={})
        add_inspections(db, first, ["src/target.py"])
        add_inspections(db, third, ["src/a.py", "src/b.py", "src/target.py"])
        add_inspections(
            db,
            fifth,
            ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/target.py"],
        )
        db.commit()

    payload = client.get("/analytics/file-localization").json()

    assert payload["total_runs_with_gold_files"] == 3
    assert payload["average_file_localization_score"] == 1.0
    assert payload["top1_accuracy"] == pytest.approx(1 / 3, abs=1e-6)
    assert payload["top3_accuracy"] == pytest.approx(2 / 3, abs=1e-6)
    assert payload["top5_accuracy"] == 1.0
    assert payload["average_files_read"] == 3.0
    assert payload["localization_by_model"][0]["top3_accuracy"] == pytest.approx(
        2 / 3, abs=1e-6
    )


def test_file_localization_calculates_edited_precision_and_recall(
    client: TestClient,
) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "precision")
        add_gold_patch(db, task, ["src/a.py", "src/b.py"])
        run = create_run(db, task, metric_values={})
        add_generated_patch(db, run, ["src/a.py", "src/unrelated.py"])
        add_inspections(db, run, ["src/a.py"])
        db.commit()

    payload = client.get("/analytics/file-localization").json()

    assert payload["average_file_localization_score"] == 0.5
    assert payload["edited_file_precision"] == 0.5
    assert payload["edited_file_recall"] == 0.5
    assert payload["average_files_edited"] == 2.0


def test_file_localization_handles_no_gold_files(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "no-gold")
        run = create_run(db, task, metric_values={})
        add_inspections(db, run, ["src/a.py"])
        db.commit()

    response = client.get("/analytics/file-localization")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_runs_with_gold_files"] == 0
    assert payload["average_file_localization_score"] == 0.0
    assert payload["most_common_missed_gold_files"] == []
    assert payload["localization_by_model"] == []
    assert payload["localization_by_repository"] == []


def test_file_localization_handles_no_pre_edit_inspections(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "no-inspection")
        add_gold_patch(db, task, ["src/target.py"])
        run = create_run(db, task, metric_values={})
        start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type="agent_tool_call",
                payload_json={
                    "tool_name": "write_file",
                    "success": True,
                    "files_modified": ["src/other.py"],
                },
                created_at=start,
            )
        )
        add_inspections(db, run, ["src/target.py"], start=start + timedelta(seconds=1))
        db.commit()

    payload = client.get("/analytics/file-localization").json()

    assert payload["total_runs_with_gold_files"] == 1
    assert payload["average_file_localization_score"] == 0.0
    assert payload["top1_accuracy"] == 0.0
    assert payload["average_files_read"] == 0.0
    assert payload["most_common_missed_gold_files"] == [
        {
            "repository_owner": "acme",
            "repository_name": "no-inspection",
            "file_path": "src/target.py",
            "missed_run_count": 1,
            "gold_run_count": 1,
            "miss_rate": 1.0,
        }
    ]


def test_file_localization_supports_filters_and_grouping(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        included_task = create_task(db, "acme", "localized")
        excluded_task = create_task(db, "other", "excluded-localized")
        add_gold_patch(db, included_task, ["src/included.py"])
        add_gold_patch(db, excluded_task, ["src/excluded.py"])
        included = create_run(
            db,
            included_task,
            provider="mock",
            model="included-model",
            started_at=datetime(2026, 2, 10, tzinfo=UTC),
            metric_values={},
        )
        excluded = create_run(
            db,
            excluded_task,
            provider="local",
            model="excluded-model",
            started_at=datetime(2026, 3, 10, tzinfo=UTC),
            metric_values={},
        )
        add_inspections(db, included, ["src/included.py"])
        add_inspections(db, excluded, ["src/wrong.py"])
        pack = attach_run_to_pack(db, included, slug="localization-pack")
        db.commit()
        filters = {
            "benchmark_pack_id": str(pack.id),
            "repository_id": str(included_task.repository_id),
            "model_provider": "mock",
            "model_name": "included-model",
            "date_from": "2026-02-01T00:00:00Z",
            "date_to": "2026-02-28T23:59:59Z",
        }

    payload = client.get("/analytics/file-localization", params=filters).json()

    assert payload["total_runs_with_gold_files"] == 1
    assert payload["average_file_localization_score"] == 1.0
    assert payload["localization_by_model"][0]["model_name"] == "included-model"
    assert payload["localization_by_repository"][0]["repository_name"] == "localized"
    for key, value in (
        ("benchmark_pack_id", str(pack.id)),
        ("repository_id", str(included_task.repository_id)),
        ("model_provider", "mock"),
        ("model_name", "included-model"),
    ):
        assert client.get("/analytics/file-localization", params={key: value}).json()[
            "total_runs_with_gold_files"
        ] == 1


def test_summary_aggregates_status_reviews_cost_and_pass_rates(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "widget")
        create_run(
            db,
            task,
            review="approved",
            metric_values={
                "file_localization_score": 1.0,
                "patch_applied": True,
                "tests_passed": True,
                "post_patch_tests_passed": True,
                "hidden_tests_passed": True,
                "hidden_tests_run_count": 2,
                "issue_resolved": True,
                "issue_specific_score": 1.0,
                "modified_files_count": 2,
                "tokens_used": 100,
                "estimated_cost": 1.0,
                "execution_time_seconds": 10.0,
            },
        )
        create_run(
            db,
            task,
            status="failed",
            review="rejected",
            metric_values={
                "file_localization_score": 0.5,
                "hidden_tests_passed": False,
                "hidden_tests_run_count": 1,
                "hidden_tests_failed_count": 1,
                "regression_detected": True,
                "modified_files_count": 4,
                "unrelated_files_count": 2,
                "tokens_used": 50,
                "estimated_cost": 0.5,
                "execution_time_seconds": 20.0,
            },
        )
        create_run(db, task, status="running", metric_values=None)
        db.commit()

    payload = client.get("/analytics/summary").json()

    assert payload["total_runs"] == 3
    assert payload["completed_runs"] == 1
    assert payload["failed_runs"] == 1
    assert payload["approved_patches"] == 1
    assert payload["rejected_patches"] == 1
    assert payload["patch_apply_rate"] == pytest.approx(0.5)
    assert payload["visible_test_pass_rate"] == pytest.approx(0.5)
    assert payload["hidden_test_pass_rate"] == pytest.approx(0.5)
    assert payload["issue_resolved_rate"] == pytest.approx(0.5)
    assert payload["regression_rate"] == pytest.approx(0.5)
    assert payload["average_file_localization_score"] == pytest.approx(0.75)
    assert payload["average_issue_specific_score"] == pytest.approx(0.5)
    assert payload["average_modified_files_count"] == pytest.approx(3.0)
    assert payload["average_unrelated_files_count"] == pytest.approx(1.0)
    assert payload["total_tokens"] == 150
    assert payload["total_cost"] == pytest.approx(1.5)
    assert payload["average_cost_per_run"] == pytest.approx(0.5)
    assert payload["average_execution_time_seconds"] == pytest.approx(15.0)


def test_provider_model_and_date_filters(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "filters")
        create_run(
            db,
            task,
            provider="mock",
            model="alpha",
            started_at=datetime(2026, 1, 10, tzinfo=UTC),
            metric_values={"tokens_used": 10},
        )
        create_run(
            db,
            task,
            provider="mock",
            model="beta",
            started_at=datetime(2026, 2, 10, tzinfo=UTC),
            metric_values={"tokens_used": 20},
        )
        create_run(
            db,
            task,
            provider="openai",
            model="alpha",
            started_at=datetime(2026, 3, 10, tzinfo=UTC),
            metric_values={"tokens_used": 30},
        )
        db.commit()

    provider = client.get("/analytics/summary", params={"model_provider": "mock"}).json()
    assert provider["total_runs"] == 2
    assert provider["total_tokens"] == 30

    model = client.get("/analytics/summary", params={"model_name": "alpha"}).json()
    assert model["total_runs"] == 2
    assert model["total_tokens"] == 40

    date_range = client.get(
        "/analytics/summary",
        params={"date_from": "2026-02-01T00:00:00Z", "date_to": "2026-02-28T23:59:59Z"},
    ).json()
    assert date_range["total_runs"] == 1
    assert date_range["total_tokens"] == 20

    invalid = client.get(
        "/analytics/summary",
        params={"date_from": "2026-03-01T00:00:00Z", "date_to": "2026-02-01T00:00:00Z"},
    )
    assert invalid.status_code == 422


def test_repository_grouping_and_filtering(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        first_task = create_task(db, "acme", "alpha")
        second_task = create_task(db, "beta", "bravo")
        create_run(db, first_task, metric_values={"tokens_used": 10})
        create_run(db, first_task, status="failed", metric_values={"tokens_used": 20})
        create_run(db, second_task, metric_values={"tokens_used": 30})
        db.commit()
        first_repository_id = first_task.repository_id

    groups = client.get("/analytics/by-repository").json()

    assert [(group["repository_owner"], group["total_runs"]) for group in groups] == [
        ("acme", 2),
        ("beta", 1),
    ]
    assert groups[0]["total_tokens"] == 30
    filtered = client.get(
        "/analytics/summary", params={"repository_id": str(first_repository_id)}
    ).json()
    assert filtered["total_runs"] == 2
    assert filtered["failed_runs"] == 1


def test_pack_analytics_uses_pack_run_provenance(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "packed")
        included = create_run(db, task, metric_values={"tokens_used": 40})
        create_run(db, task, metric_values={"tokens_used": 100})
        pack = attach_run_to_pack(db, included, slug="core-pack")
        db.commit()
        pack_id = pack.id

    summary = client.get("/analytics/summary", params={"benchmark_pack_id": str(pack_id)}).json()
    groups = client.get("/analytics/by-pack").json()

    assert summary["total_runs"] == 1
    assert summary["total_tokens"] == 40
    assert len(groups) == 1
    assert groups[0]["benchmark_pack_id"] == str(pack_id)
    assert groups[0]["pack_slug"] == "core-pack"
    assert groups[0]["total_runs"] == 1
    assert groups[0]["total_tokens"] == 40


def test_empty_model_leaderboard(client: TestClient) -> None:
    response = client.get("/analytics/model-leaderboard")

    assert response.status_code == 200
    assert response.json() == []


def test_model_leaderboard_groups_runs_and_assigns_ranks(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "leaderboard")
        for tokens in (90, 110):
            create_run(
                db,
                task,
                provider="openai",
                model="strong-model",
                metric_values={
                    "issue_resolved": True,
                    "post_patch_tests_passed": True,
                    "hidden_tests_passed": True,
                    "hidden_tests_run_count": 1,
                    "file_localization_score": 0.9,
                    "issue_specific_score": 1.0,
                    "estimated_cost": 0.2,
                    "tokens_used": tokens,
                    "execution_time_seconds": 30.0,
                    "modified_files_count": 2,
                },
            )
        for tokens in (40, 60):
            create_run(
                db,
                task,
                provider="local",
                model="fast-model",
                status="failed",
                metric_values={
                    "file_localization_score": 0.3,
                    "estimated_cost": 0.05,
                    "tokens_used": tokens,
                    "execution_time_seconds": 10.0,
                    "modified_files_count": 4,
                    "unrelated_files_count": 2,
                },
            )
        db.commit()

    response = client.get("/analytics/model-leaderboard")

    assert response.status_code == 200
    rows = response.json()
    assert [(row["model_provider"], row["model_name"]) for row in rows] == [
        ("openai", "strong-model"),
        ("local", "fast-model"),
    ]
    strong, fast = rows
    assert strong["total_runs"] == 2
    assert strong["completed_runs"] == 2
    assert strong["average_tokens_per_run"] == pytest.approx(100.0)
    assert strong["rank_by_issue_resolved"] == 1
    assert strong["rank_by_localization"] == 1
    assert strong["rank_by_cost"] == 2
    assert strong["rank_by_speed"] == 2
    assert fast["failed_runs"] == 2
    assert fast["rank_by_issue_resolved"] == 2
    assert fast["rank_by_localization"] == 2
    assert fast["rank_by_cost"] == 1
    assert fast["rank_by_speed"] == 1
    assert strong["composite_score"] > fast["composite_score"]


def test_model_leaderboard_min_runs_filter(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "minimum")
        create_run(db, task, model="established", metric_values={})
        create_run(db, task, model="established", metric_values={})
        create_run(db, task, model="newcomer", metric_values={})
        db.commit()

    response = client.get("/analytics/model-leaderboard", params={"min_runs": 2})

    assert response.status_code == 200
    assert [row["model_name"] for row in response.json()] == ["established"]
    assert client.get("/analytics/model-leaderboard", params={"min_runs": 0}).status_code == 422


def test_model_leaderboard_uses_competition_ranks_for_ties(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        task = create_task(db, "acme", "ties")
        shared_metrics = {
            "issue_resolved": True,
            "post_patch_tests_passed": True,
            "file_localization_score": 0.8,
            "estimated_cost": 0.1,
            "execution_time_seconds": 12.0,
            "unrelated_files_count": 1,
        }
        create_run(db, task, provider="alpha", model="same", metric_values=shared_metrics)
        create_run(db, task, provider="beta", model="same", metric_values=shared_metrics)
        create_run(
            db,
            task,
            provider="gamma",
            model="lower",
            metric_values={"file_localization_score": 0.1, "estimated_cost": 0.2},
        )
        db.commit()

    rows = client.get("/analytics/model-leaderboard").json()
    tied = [row for row in rows if row["model_name"] == "same"]
    lower = next(row for row in rows if row["model_name"] == "lower")

    assert len(tied) == 2
    assert {row["rank_by_issue_resolved"] for row in tied} == {1}
    assert {row["rank_by_localization"] for row in tied} == {1}
    assert {row["composite_score"] for row in tied} == {tied[0]["composite_score"]}
    assert lower["rank_by_issue_resolved"] == 3
    assert lower["rank_by_localization"] == 3


def test_model_leaderboard_pack_repository_and_date_filters(client: TestClient) -> None:
    with TestingSessionLocal() as db:
        first_task = create_task(db, "acme", "filtered-board")
        second_task = create_task(db, "other", "excluded-board")
        included = create_run(
            db,
            first_task,
            model="included",
            started_at=datetime(2026, 2, 1, tzinfo=UTC),
            metric_values={"issue_resolved": True},
        )
        create_run(
            db,
            first_task,
            model="outside-pack",
            started_at=datetime(2026, 2, 2, tzinfo=UTC),
            metric_values={},
        )
        create_run(
            db,
            second_task,
            model="other-repository",
            started_at=datetime(2026, 3, 1, tzinfo=UTC),
            metric_values={},
        )
        pack = attach_run_to_pack(db, included, slug="leaderboard-pack")
        db.commit()
        pack_id = pack.id
        repository_id = first_task.repository_id

    pack_rows = client.get(
        "/analytics/model-leaderboard", params={"benchmark_pack_id": str(pack_id)}
    ).json()
    repository_rows = client.get(
        "/analytics/model-leaderboard", params={"repository_id": str(repository_id)}
    ).json()
    date_rows = client.get(
        "/analytics/model-leaderboard",
        params={"date_from": "2026-03-01T00:00:00Z"},
    ).json()

    assert [row["model_name"] for row in pack_rows] == ["included"]
    assert {row["model_name"] for row in repository_rows} == {"included", "outside-pack"}
    assert [row["model_name"] for row in date_rows] == ["other-repository"]
