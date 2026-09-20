from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from alembic import command
from app.agents.prompts import render_agent_prompts
from app.benchmark_imports import BenchmarkImportService
from app.benchmark_tasks import validate_benchmark_task
from app.core.config import settings
from app.db.base import Base
from app.db.migrations import make_alembic_config
from app.db.session import get_db
from app.main import create_app
from app.models import (
    AgentRun,
    BenchmarkImport,
    BenchmarkPack,
    BenchmarkPackTask,
    BenchmarkTask,
    GoldPatch,
    HiddenEvalTest,
    Repository,
)

HEADERS = {"X-Operator-Token": "test-import-operator"}
ROOT = Path(__file__).resolve().parents[2]


def diff(path: str, marker: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+{marker}\n"
    )


def record(task_id: str = "example__project-1", **changes) -> dict:
    return {
        "task_id": task_id,
        "repo": "example/project",
        "problem_statement": "Fix boundary handling\nThe input fails at zero.",
        "base_commit": "a" * 40,
        **changes,
    }


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setattr(settings, "trusted_operator_token", HEADERS["X-Operator-Token"])
    monkeypatch.setattr(settings, "database_auto_create_tables", False)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def import_records(db, records):
    return BenchmarkImportService(db).import_bytes(json.dumps(records).encode(), format="json")


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_file_import_supports_formats_and_reuses_repository(db, tmp_path, format):
    rows = [
        record(issue_number=42, created_at="2020-01-01T00:00:00Z"),
        record("second-task", repo="Example/Project"),
    ]
    content = json.dumps(rows) if format == "json" else "\n".join(map(json.dumps, rows))
    path = tmp_path / f"tasks.{format}"
    path.write_text(content, encoding="utf-8-sig")
    result = BenchmarkImportService(db).import_file(path)
    assert (result.imported_count, result.failed_count, result.skipped_count) == (2, 0, 0)
    assert len(result.created_task_ids) == 2
    assert db.scalar(select(func.count()).select_from(Repository)) == 1
    tasks = [db.get(BenchmarkTask, id) for id in result.created_task_ids]
    assert tasks[0].issue_number == 42
    assert tasks[1].issue_number is None
    assert all(task.status == "draft" and task.test_commands == [] for task in tasks)
    assert tasks[0].import_record.source_created_at.replace(tzinfo=UTC) == datetime(
        2020, 1, 1, tzinfo=UTC
    )
    assert tasks[0].issue_body == rows[0]["problem_statement"]


@pytest.mark.parametrize("field", ["task_id", "problem_statement", "base_commit", "repo"])
def test_missing_required_fields(db, field):
    invalid = record()
    del invalid[field]
    result = import_records(db, [invalid])
    assert result.failed_count == 1
    assert result.errors[0].row == 1
    assert result.created_task_ids == []
    assert db.scalar(select(func.count()).select_from(BenchmarkTask)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"problem_statement": " \n "},
        {"base_commit": " "},
        {"issue_number": True},
        {"repo": "../project"},
        {"repo_url": "http://github.com/example/project"},
        {"repo_url": "https://github.com/example/other"},
        {"repo_url": "https://github.com/example/project/issues/42"},
        {"repo_url": "https://secret@github.com/example/project"},
        {"repo_url": "https://localhost/example/project"},
        {"fail_to_pass": "not a list"},
        {"pass_to_pass": [" "]},
    ],
)
def test_invalid_fields_rejected(db, changes):
    assert import_records(db, [record(**changes)]).failed_count == 1


def test_url_only_record_is_supported(db):
    value = record(repo_url="https://github.com/Example/Project.git")
    del value["repo"]
    result = import_records(db, [value])
    assert result.imported_count == 1
    assert db.get(BenchmarkTask, result.created_task_ids[0]).repository.url == (
        "https://github.com/example/project"
    )


def test_duplicate_ids_skipped_within_file_and_across_imports(db):
    result = import_records(db, [record(), record(problem_statement="Must not overwrite")])
    again = import_records(db, [record()])
    assert (result.imported_count, result.skipped_count, result.failed_count) == (1, 1, 0)
    assert result.errors[0].row == 2
    assert result.errors[0].task_id == record()["task_id"]
    assert result.errors[0].outcome == "skipped"
    assert again.skipped_count == 1
    assert db.scalar(select(func.count()).select_from(BenchmarkImport)) == 1
    assert db.get(BenchmarkTask, result.created_task_ids[0]).issue_title == "Fix boundary handling"


def test_malformed_jsonl_row_does_not_stop_other_rows(db):
    payload = (
        json.dumps(record()) + '\n\n{"patch":"PRIVATE"\n' + json.dumps(record("next"))
    ).encode()
    result = BenchmarkImportService(db).import_bytes(payload, format="jsonl")
    assert result.imported_count == 2
    assert result.failed_count == 1
    assert result.errors[0].row == 3
    assert "PRIVATE" not in result.model_dump_json()


def test_jsonl_unicode_separator_inside_statement_is_not_a_record_boundary(db):
    value = record(problem_statement="First paragraph\u2028Second paragraph")
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    result = BenchmarkImportService(db).import_bytes(payload, format="jsonl")
    assert result.imported_count == 1
    assert (
        db.get(BenchmarkTask, result.created_task_ids[0]).issue_body == value["problem_statement"]
    )


def test_failed_row_is_atomic_and_later_rows_continue(db, monkeypatch):
    service = BenchmarkImportService(db)
    create = service._create_task

    def create_with_failure(value, **kwargs):
        id = create(value, **kwargs)
        if value.task_id == "broken":
            raise SQLAlchemyError("PRIVATE PATCH CONTENT")
        return id

    monkeypatch.setattr(service, "_create_task", create_with_failure)
    result = service.import_bytes(
        json.dumps(
            [
                record(),
                record("broken", repo="other/repo", patch=diff("x.py", "PRIVATE")),
                record("last"),
            ]
        ).encode()
    )
    assert result.imported_count == 2
    assert result.failed_count == 1
    assert db.scalar(select(func.count()).select_from(Repository)) == 1
    assert db.scalar(select(func.count()).select_from(BenchmarkTask)) == 2
    assert db.scalar(select(func.count()).select_from(GoldPatch)) == 0
    assert "PRIVATE" not in result.model_dump_json()


def test_gold_and_hidden_metadata_are_stored_only_in_trusted_models(db, client):
    value = record(
        patch=diff("src/private_fix.py", "GOLD_SOLUTION_SECRET"),
        test_patch=diff("tests/private_check.py", "HIDDEN_TEST_SECRET"),
        fail_to_pass=["PRIVATE_FAIL_TEST; do not execute"],
        pass_to_pass=["PRIVATE_PASS_TEST"],
        environment_setup_commit="b" * 40,
        hints_text="PRIVATE_HINT",
    )
    response = client.post("/benchmark-imports", json=[value], headers=HEADERS)
    assert response.status_code == 200
    task_id = UUID(response.json()["created_task_ids"][0])
    task = db.get(BenchmarkTask, task_id)
    assert task.gold_patch.patch_text == value["patch"]
    assert task.gold_patch.changed_files == ["src/private_fix.py"]
    assert task.gold_patch.test_files == ["tests/private_check.py"]
    hidden = task.hidden_eval_tests[0]
    assert hidden.patch_text == value["test_patch"]
    assert hidden.evaluation_metadata == {
        "fail_to_pass": value["fail_to_pass"],
        "pass_to_pass": value["pass_to_pass"],
    }
    assert hidden.enabled is False and hidden.commands == []
    assert task.import_record.environment_setup_commit == "b" * 40
    assert task.import_record.hints_text == "PRIVATE_HINT"

    prompts = render_agent_prompts(
        task=task,
        repository=task.repository,
        allowed_tools=["read_file"],
        configured_test_commands=task.test_commands,
        max_steps=4,
        max_tool_errors=2,
        command_timeout_seconds=30,
        include_issue_comments=True,
        enable_test_tool=True,
        run_mode="tool_loop",
    )
    public = client.get("/api/v1/benchmark-tasks")
    assert public.status_code == 200
    assert public.json()[0]["issue_number"] is None
    run = AgentRun(benchmark_task_id=task_id, model_provider="mock", model_name="mock")
    db.add(run)
    db.commit()
    detail = client.get(f"/agent-runs/{run.id}")
    assert detail.status_code == 200
    for surface in (
        response.text,
        public.text,
        detail.text,
        json.dumps(prompts.redacted_preview()),
        str(prompts.messages()),
    ):
        for marker in (
            "GOLD_SOLUTION_SECRET",
            "HIDDEN_TEST_SECRET",
            "PRIVATE_FAIL_TEST",
            "PRIVATE_PASS_TEST",
            "PRIVATE_HINT",
            "private_fix.py",
            "private_check.py",
        ):
            assert marker not in surface

    assert client.get(f"/benchmark-tasks/{task_id}/hidden-tests").status_code == 403
    assert client.get(f"/evaluation/benchmark-tasks/{task_id}/gold-patch").status_code == 403
    assert (
        client.get(f"/evaluation/benchmark-tasks/{task_id}/gold-patch", headers=HEADERS).json()[
            "patch_text"
        ]
        == value["patch"]
    )
    private = client.get(f"/benchmark-tasks/{task_id}/hidden-tests", headers=HEADERS)
    assert private.json()[0]["evaluation_metadata"]["pass_to_pass"] == value["pass_to_pass"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"test_patch": diff("tests/test_only.py", "assert True")},
        {"fail_to_pass": ["test_one"]},
        {"pass_to_pass": ["test_two"]},
        {"fail_to_pass": []},
    ],
)
def test_hidden_entry_created_for_any_evaluation_payload(db, metadata):
    result = import_records(db, [record(**metadata)])
    task = db.get(BenchmarkTask, result.created_task_ids[0])
    assert len(task.hidden_eval_tests) == 1
    assert not task.hidden_eval_tests[0].enabled
    assert task.gold_patch is None


def test_plain_record_does_not_create_gold_or_hidden_data(db):
    result = import_records(db, [record()])
    task = db.get(BenchmarkTask, result.created_task_ids[0])
    assert task.gold_patch is None and task.hidden_eval_tests == []


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/absolute"])
def test_unsafe_patch_paths_rejected_without_orphan_rows(db, path):
    result = import_records(db, [record(test_patch=diff(path, "PRIVATE"))])
    assert result.failed_count == 1
    assert "PRIVATE" not in result.model_dump_json()
    assert db.scalar(select(func.count()).select_from(Repository)) == 0


def test_imported_task_readiness_requires_configured_tests_not_a_github_pr(db):
    result = import_records(db, [record(patch=diff("src/core.py", "fixed"))])
    task = db.get(BenchmarkTask, result.created_task_ids[0])
    assert not validate_benchmark_task(db, task).valid
    task.test_commands = ["python -m pytest"]
    db.commit()
    assert validate_benchmark_task(db, task).valid


def test_api_requires_trusted_operator_and_accepts_jsonl(client, db, monkeypatch):
    assert client.post("/benchmark-imports", json=[record()]).status_code == 403
    assert (
        client.post(
            "/benchmark-imports", json=[record()], headers={"X-Operator-Token": "wrong"}
        ).status_code
        == 403
    )
    assert db.scalar(select(func.count()).select_from(BenchmarkTask)) == 0
    response = client.post(
        "/benchmark-imports",
        content=json.dumps(record()),
        headers={**HEADERS, "Content-Type": "application/x-ndjson"},
    )
    assert response.status_code == 200 and response.json()["imported_count"] == 1
    assert (
        client.post(
            "/benchmark-imports", content="[]", headers={**HEADERS, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    monkeypatch.setattr(settings, "trusted_operator_token", None)
    assert client.post("/benchmark-imports", json=[], headers=HEADERS).status_code == 503


def test_api_and_service_size_limits_and_validation_redact_payloads(client, db, monkeypatch):
    monkeypatch.setattr("app.api.routes.benchmark_imports.MAX_IMPORT_BYTES", 10)
    assert client.post("/benchmark-imports", json=[record()], headers=HEADERS).status_code == 413
    monkeypatch.setattr("app.benchmark_imports.service.MAX_IMPORT_BYTES", 10)
    assert BenchmarkImportService(db).import_bytes(b"x" * 11).errors[0].code == "file_too_large"


@pytest.mark.parametrize(
    "payload,format",
    [
        (b"[{", "json"),
        (b"{}", "json"),
        (b"\xff", "jsonl"),
        (b"  ", "auto"),
    ],
)
def test_bad_documents_return_reportable_errors(db, payload, format):
    result = BenchmarkImportService(db).import_bytes(payload, format=format)
    assert result.failed_count == 1 and result.errors[0].row is None


def test_validation_errors_do_not_echo_gold_or_unrecognized_fields(db):
    result = import_records(db, [record(patch={"GOLD_SECRET": "body"}, GOLD_FIELD="PRIVATE")])
    assert result.failed_count == 1
    assert "GOLD_SECRET" not in result.model_dump_json()
    assert "GOLD_FIELD" not in result.model_dump_json()
    assert "PRIVATE" not in result.model_dump_json()


def test_import_script_smoke_and_migration_roundtrip(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'imports.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with Session(engine) as db:
        pack = BenchmarkPack(name="Script pack", slug="script-pack", version="1")
        db.add(pack)
        db.commit()
        pack_id = pack.id
    file = tmp_path / "tasks.jsonl"
    file.write_text(
        json.dumps(record(test_patch=diff("tests/test_x.py", "SECRET"))), encoding="utf-8"
    )
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "DATABASE_AUTO_CREATE_TABLES": "false",
        "ENABLE_REAL_MODEL_CALLS": "false",
        "ENABLE_REAL_EMBEDDINGS": "false",
        "ENABLE_LOCAL_MODEL_CALLS": "false",
    }
    args = [
        sys.executable,
        str(ROOT / "scripts/import_benchmark_tasks.py"),
        str(file),
        "--pack-id",
        str(pack_id),
    ]
    first = subprocess.run(
        args, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["imported_count"] == 1
    again = subprocess.run(
        args, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["skipped_count"] == 1
    file.write_text('{"patch": "SECRET"}', encoding="utf-8")
    invalid = subprocess.run(
        args, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert invalid.returncode == 1 and json.loads(invalid.stdout)["failed_count"] == 1
    assert "SECRET" not in invalid.stdout + invalid.stderr
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BenchmarkImport)) == 1
        assert db.scalar(select(HiddenEvalTest)).patch_text is not None
        membership = db.scalar(select(BenchmarkPackTask))
        assert membership is not None and membership.benchmark_pack_id == pack_id
        assert membership.order_index == 0
    command.downgrade(config, "20260916_0008")
    assert "benchmark_imports" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert db.scalar(select(BenchmarkTask)).issue_number == 0
    engine.dispose()
