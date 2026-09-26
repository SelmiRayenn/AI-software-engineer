import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path, PurePath

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    GeneratedPatch,
    GoldPatch,
    IndexedFile,
    Repository,
    RepositoryIndex,
)
from app.targeted_tests import TargetedTestSelectionService
from app.test_execution import TestExecutionService as ExecutionService

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def test_changed_test_file_selects_recognized_target(db: Session) -> None:
    run = _run(db, commands=["pytest -q"], changed_files=["tests/test_calculator.py"])

    selection = TargetedTestSelectionService(db, run.id).select(max_commands=3)

    assert selection.selected_commands == ["pytest -q tests/test_calculator.py"]
    assert selection.confidence == "high"
    assert selection.fallback_to_full_suite is False
    event = db.scalar(select(AgentEvent).where(AgentEvent.event_type == "targeted_tests_selected"))
    assert event.payload_json["selected_commands"] == selection.selected_commands


def test_source_file_maps_to_related_indexed_test(db: Session) -> None:
    run = _run(db, commands=["python -m pytest -q"], changed_files=["src/calculator.py"])
    _index_files(db, run, ["src/calculator.py", "tests/test_calculator.py"])

    selection = TargetedTestSelectionService(db, run.id).select(max_commands=3)

    assert selection.selected_commands == ["python -m pytest -q tests/test_calculator.py"]
    assert selection.confidence == "medium"
    assert selection.selection_reason.startswith("Related test files")


def test_unrecognized_commands_fall_back_to_full_suite(db: Session) -> None:
    configured = ["make verify", "./scripts/check-project"]
    run = _run(db, commands=configured, changed_files=["tests/test_calculator.py"])

    selection = TargetedTestSelectionService(db, run.id).select(max_commands=1)

    assert selection.selected_commands == configured
    assert selection.fallback_to_full_suite is True
    assert selection.confidence == "low"


def test_targeted_command_limit_is_enforced(db: Session) -> None:
    tests = [f"tests/test_case_{index}.py" for index in range(5)]
    run = _run(db, commands=["pytest"], changed_files=tests)

    selection = TargetedTestSelectionService(db, run.id).select(max_commands=3)

    assert selection.selected_commands == [f"pytest {path}" for path in tests[:3]]


def test_gold_test_files_require_trusted_mode_and_never_create_commands(db: Session) -> None:
    configured = ["pytest -q tests/test_public.py", "pytest -q tests/test_gold_fix.py"]
    run = _run(db, commands=configured, changed_files=["src/calculator.py"])
    db.add(
        GoldPatch(
            benchmark_task_id=run.benchmark_task_id,
            changed_files=["src/calculator.py"],
            patch_text="TRUSTED_PATCH",
            test_files=["tests/test_gold_fix.py"],
        )
    )
    db.commit()

    public = TargetedTestSelectionService(db, run.id).select(
        max_commands=3, trusted_gold_files=False, persist=False
    )
    trusted = TargetedTestSelectionService(db, run.id).select(
        max_commands=3, trusted_gold_files=True, persist=False
    )

    assert public.selected_commands == configured and public.fallback_to_full_suite
    assert trusted.selected_commands == ["pytest -q tests/test_gold_fix.py"]
    assert trusted.selected_commands[0] in configured
    assert trusted.fallback_to_full_suite is False


def test_selected_commands_are_safely_derived_from_configured_runner(db: Session) -> None:
    configured = "npx vitest run --reporter=dot"
    run = _run(db, commands=[configured], changed_files=["src/widget.test.ts"])

    selection = TargetedTestSelectionService(db, run.id).select(max_commands=3)

    assert selection.selected_commands == [f"{configured} src/widget.test.ts"]
    assert selection.selected_commands[0].startswith(configured + " ")


def test_endpoint_selects_targeted_tests(client: TestClient, db: Session) -> None:
    run = _run(db, commands=["pytest -q"], changed_files=["tests/test_api.py"])

    response = client.post(
        f"/agent-runs/{run.id}/tests/select-targeted",
        json={"targeted_tests_max_commands": 1},
    )

    assert response.status_code == 200
    assert response.json() == {
        "selected_commands": ["pytest -q tests/test_api.py"],
        "selection_reason": (
            "Changed or inspected test files map to recognized configured test runners."
        ),
        "confidence": "high",
        "fallback_to_full_suite": False,
    }


def test_endpoint_requires_operator_for_gold_hints(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(db, commands=["pytest -q"])
    monkeypatch.setattr(settings, "trusted_operator_token", "operator-secret")

    forbidden = client.post(
        f"/agent-runs/{run.id}/tests/select-targeted",
        json={"targeted_tests_trusted_gold_files": True},
    )

    assert forbidden.status_code == 403


def test_post_patch_execution_uses_targeted_selection(
    db: Session,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_selected.py").write_text(
        "def test_selected():\n    assert True\n",
        encoding="utf-8",
    )
    _init_git(workspace)
    command = f'"{sys.executable}" -m pytest -q'
    run = _run(
        db,
        commands=[command],
        changed_files=["tests/test_selected.py"],
        workspace=workspace,
    )

    phase = ExecutionService(db=db, agent_run_id=run.id).run_post_patch_tests(
        enable_targeted_tests=True,
        targeted_tests_max_commands=1,
        finalize_run=False,
    )

    assert phase.passed is True
    assert len(phase.test_results) == 1
    assert phase.test_results[0].command.endswith(" tests/test_selected.py")


def _run(
    db: Session,
    *,
    commands: list[str],
    changed_files: list[str] | None = None,
    workspace: Path | None = None,
) -> AgentRun:
    repository = Repository(
        owner="example",
        name=f"targeted-{db.query(Repository).count()}",
        url=f"https://github.com/example/targeted-{db.query(Repository).count()}",
        language="Python",
    )
    db.add(repository)
    db.flush()
    task = BenchmarkTask(
        repository_id=repository.id,
        issue_title="Select relevant tests",
        issue_body="Run the smallest safe test subset.",
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=commands,
        status="ready",
    )
    db.add(task)
    db.flush()
    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="mock",
        status="running",
        workspace_id="targeted-tests" if workspace else None,
        workspace_path=str(workspace) if workspace else None,
    )
    db.add(run)
    db.flush()
    if changed_files is not None:
        db.add(
            GeneratedPatch(
                agent_run_id=run.id,
                patch_text="",
                changed_files=changed_files,
                is_selected=True,
            )
        )
    db.commit()
    db.refresh(run)
    return run


def _index_files(db: Session, run: AgentRun, paths: list[str]) -> None:
    index = RepositoryIndex(
        agent_run_id=run.id,
        workspace_id="targeted-index",
        index_version=1,
        indexed_at=datetime.now(UTC),
        file_count=len(paths),
        total_size_bytes=len(paths),
        checksum="a" * 64,
        skipped_counts={},
    )
    db.add(index)
    db.flush()
    for path in paths:
        suffix = PurePath(path).suffix
        db.add(
            IndexedFile(
                repository_index_id=index.id,
                file_path=path,
                extension=suffix,
                size_bytes=1,
                language="Python",
                file_type="test" if "test" in path else "source",
                preview="",
                content="",
                checksum="b" * 64,
                imports=[],
                python_parse_error=False,
            )
        )
    db.commit()


def _init_git(workspace: Path) -> None:
    for args in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "."],
        ["commit", "-m", "base"],
    ):
        completed = subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
