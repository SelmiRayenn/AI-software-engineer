from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.benchmark_validation import build_benchmark_pack_validation_report
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import (
    BenchmarkImport,
    BenchmarkPack,
    BenchmarkPackTask,
    BenchmarkTask,
    GoldPatch,
    HiddenEvalTest,
    Repository,
)

GOLD_SECRET = "PRIVATE_GOLD_PATCH"
HIDDEN_SECRET = "PRIVATE_HIDDEN_COMMAND"


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
def client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def create_task(
    db: Session,
    *,
    status: str = "ready",
    setup_commands: object | None = None,
    test_commands: object | None = None,
    base_commit: str = "a" * 40,
    with_gold: bool = True,
    gold_test_files: list[str] | None = None,
    with_hidden: bool = False,
    imported: bool = False,
) -> BenchmarkTask:
    repository = Repository(
        name=f"repo-{uuid4().hex[:8]}",
        owner="example",
        url=f"https://github.com/example/repo-{uuid4().hex[:8]}",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=None if imported else 42,
        issue_title="Fix boundary behavior",
        issue_body="The boundary case returns an incorrect result.",
        pull_request_number=None if imported else 43,
        base_commit=base_commit,
        setup_commands=["python -m pip install -e ."] if setup_commands is None else setup_commands,
        test_commands=["pytest -q"] if test_commands is None else test_commands,
        status=status,
    )
    db.add(task)
    db.flush()
    if with_gold:
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=["src/core.py"],
                patch_text=GOLD_SECRET,
                test_files=gold_test_files or [],
            )
        )
    if with_hidden:
        db.add(
            HiddenEvalTest(
                benchmark_task_id=task.id,
                name="private suite",
                commands=[HIDDEN_SECRET],
                enabled=True,
            )
        )
    if imported:
        db.add(BenchmarkImport(task_id=f"example__task-{uuid4().hex}", benchmark_task_id=task.id))
    db.commit()
    db.refresh(task)
    return task


def create_pack(db: Session, tasks: list[BenchmarkTask], *, complete_metadata: bool = True):
    pack = BenchmarkPack(name="Validation pack", slug=f"pack-{uuid4().hex}", version="1")
    db.add(pack)
    db.flush()
    for index, task in enumerate(tasks):
        db.add(
            BenchmarkPackTask(
                benchmark_pack_id=pack.id,
                benchmark_task_id=task.id,
                order_index=index,
                difficulty="medium" if complete_metadata else None,
                tags=["python"] if complete_metadata else [],
            )
        )
    db.commit()
    db.refresh(pack)
    return pack


def codes(payload: dict, severity: str | None = None) -> set[str]:
    return {
        item["code"]
        for item in payload["items"]
        if severity is None or item["severity"] == severity
    }


def test_valid_task_report_is_ready_and_hides_trusted_data(client, db):
    task = create_task(
        db,
        gold_test_files=["tests/private_gold_test.py"],
        with_hidden=True,
    )
    response = client.get(f"/benchmark-tasks/{task.id}/validation-report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "ready"
    assert payload["summary"] == {
        "total_count": 1,
        "info_count": 1,
        "warning_count": 0,
        "error_count": 0,
    }
    assert payload["statistics"]["imported"] is False
    assert GOLD_SECRET not in response.text and HIDDEN_SECRET not in response.text


def test_task_missing_commands_is_blocked(client, db):
    task = create_task(db, setup_commands=[], test_commands=[])
    payload = client.get(f"/benchmark-tasks/{task.id}/validation-report").json()
    assert payload["overall_status"] == "blocked"
    assert "setup_commands_missing" in codes(payload, "warning")
    assert "test_commands_missing" in codes(payload, "error")
    assert "ready_status_inconsistent" in codes(payload, "error")
    assert payload["summary"]["error_count"] == 2


def test_task_missing_base_commit_is_blocked(client, db):
    task = create_task(db, base_commit="")
    payload = client.get(f"/benchmark-tasks/{task.id}/validation-report").json()
    assert payload["overall_status"] == "blocked"
    assert "base_commit_missing" in codes(payload, "error")


def test_imported_draft_reports_optional_gold_and_disabled_hidden_metadata(client, db):
    task = create_task(
        db,
        status="draft",
        with_gold=False,
        with_hidden=False,
        imported=True,
        setup_commands=[],
        test_commands=[],
    )
    task.hidden_eval_tests.append(
        HiddenEvalTest(
            name="imported metadata",
            commands=[],
            evaluation_metadata={"fail_to_pass": ["tests/test_x.py::test_x"]},
            enabled=False,
        )
    )
    db.commit()
    payload = client.get(f"/benchmark-tasks/{task.id}/validation-report").json()
    assert payload["statistics"]["imported"] is True
    assert "gold_patch_missing" in codes(payload, "warning")
    assert "hidden_tests_disabled" in codes(payload, "warning")
    assert "test_commands_missing" in codes(payload, "error")


def test_pack_with_no_tasks_is_blocked(client, db):
    pack = create_pack(db, [])
    payload = client.get(f"/benchmark-packs/{pack.id}/validation-report").json()
    assert payload["overall_status"] == "blocked"
    assert "pack_empty" in codes(payload, "error")
    assert payload["statistics"]["task_count"] == 0


def test_pack_with_draft_task_has_status_counts_and_warning(client, db):
    task = create_task(db, status="draft", with_hidden=True)
    pack = create_pack(db, [task])
    payload = client.get(f"/benchmark-packs/{pack.id}/validation-report").json()
    assert payload["overall_status"] == "blocked"
    assert payload["statistics"]["ready_task_count"] == 0
    assert payload["statistics"]["draft_task_count"] == 1
    assert "non_ready_tasks_present" in codes(payload, "warning")
    assert "pack_has_no_ready_tasks" in codes(payload, "error")


def test_duplicate_order_index_is_blocking_even_if_database_constraint_is_bypassed(db):
    first = create_task(db, with_hidden=True)
    second = create_task(db, with_hidden=True)
    pack = create_pack(db, [first, second])
    assert len(pack.task_memberships) == 2
    pack.task_memberships[1].order_index = pack.task_memberships[0].order_index
    with db.no_autoflush:
        report = build_benchmark_pack_validation_report(pack)
    db.rollback()
    assert report.overall_status == "blocked"
    assert "duplicate_order_indexes" in {item.code for item in report.items}


def test_hidden_test_coverage_warning_and_statistics(client, db):
    covered = create_task(db, with_hidden=True)
    uncovered = create_task(db)
    pack = create_pack(db, [covered, uncovered])
    payload = client.get(f"/benchmark-packs/{pack.id}/validation-report").json()
    assert payload["overall_status"] == "warning"
    assert "hidden_test_coverage_incomplete" in codes(payload, "warning")
    assert payload["statistics"]["hidden_test_task_count"] == 1
    assert payload["statistics"]["hidden_test_coverage"] == 0.5


def test_pack_overall_status_ready_warning_and_blocked(client, db):
    ready_task = create_task(db, with_hidden=True)
    ready_pack = create_pack(db, [ready_task])
    ready = client.get(f"/benchmark-packs/{ready_pack.id}/validation-report").json()
    assert ready["overall_status"] == "ready"
    assert ready["summary"]["error_count"] == ready["summary"]["warning_count"] == 0

    warning_task = create_task(db, with_hidden=True)
    warning_pack = create_pack(db, [warning_task], complete_metadata=False)
    warning = client.get(f"/benchmark-packs/{warning_pack.id}/validation-report").json()
    assert warning["overall_status"] == "warning"
    assert {"difficulty_coverage_incomplete", "tag_coverage_incomplete"}.issubset(
        codes(warning, "warning")
    )

    blocked_task = create_task(db, with_hidden=True, test_commands=[])
    blocked_pack = create_pack(db, [blocked_task])
    blocked = client.get(f"/benchmark-packs/{blocked_pack.id}/validation-report").json()
    assert blocked["overall_status"] == "blocked"
    assert "pack_test_commands_missing" in codes(blocked, "error")


def test_missing_reports_return_404(client):
    assert client.get(f"/benchmark-tasks/{uuid4()}/validation-report").status_code == 404
    assert client.get(f"/benchmark-packs/{uuid4()}/validation-report").status_code == 404
