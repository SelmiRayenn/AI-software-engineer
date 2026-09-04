import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
from app.db.base import Base
from app.models import AgentEvent, AgentRun, BenchmarkTask, Repository
from app.models import TestResult as ResultRecord

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
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / ".benchmark_gold").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_calculator.py").write_text(
        "from src.calculator import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / ".benchmark_gold" / "gold_patch.diff").write_text(
        "hidden solution",
        encoding="utf-8",
    )
    return root


def test_list_files_is_safe_and_skips_hidden_gold(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    result = tools.list_files()

    assert "src/calculator.py" in result.files
    assert "tests/test_calculator.py" in result.files
    assert ".benchmark_gold/gold_patch.diff" not in result.files
    assert last_event(db).payload_json["success"] is True


def test_search_code_finds_limited_workspace_matches(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    result = tools.search_code("return")

    assert len(result.matches) == 1
    assert result.matches[0].file_path == "src/calculator.py"
    assert result.matches[0].line_number == 2
    assert last_event(db).payload_json["tool_name"] == "search_code"


def test_read_file_allows_workspace_file(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    result = tools.read_file("src/calculator.py")

    assert result.file_path == "src/calculator.py"
    assert "def add" in result.content
    event = last_event(db).payload_json
    assert event["files_read"] == ["src/calculator.py"]


def test_path_traversal_is_rejected_and_logged(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    with pytest.raises(ToolSafetyError):
        tools.read_file("../secret.txt")

    event = last_event(db).payload_json
    assert event["tool_name"] == "read_file"
    assert event["success"] is False
    assert "Path traversal" in event["error_message"]


def test_write_outside_workspace_is_rejected_and_logged(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    with pytest.raises(ToolSafetyError):
        tools.write_file("..\\outside.txt", "nope")

    assert not (workspace.parent / "outside.txt").exists()
    event = last_event(db).payload_json
    assert event["tool_name"] == "write_file"
    assert event["success"] is False


def test_write_to_hidden_gold_solution_path_is_rejected(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace)

    with pytest.raises(ToolSafetyError):
        tools.write_file(".benchmark_gold/gold_patch.diff", "tampered")

    assert (workspace / ".benchmark_gold" / "gold_patch.diff").read_text(
        encoding="utf-8"
    ) == "hidden solution"
    assert last_event(db).payload_json["success"] is False


def test_run_tests_only_allows_configured_commands(db: Session, workspace: Path) -> None:
    tools = create_tools(db, workspace, allowed_test_commands=["python -c \"print('ok')\""])

    with pytest.raises(ToolSafetyError):
        tools.run_tests("pytest")

    event = last_event(db).payload_json
    assert event["tool_name"] == "run_tests"
    assert event["success"] is False
    assert "allowed test command" in event["error_message"]


def test_run_tests_executes_allowed_command_and_records_result(
    db: Session,
    workspace: Path,
) -> None:
    command = f'"{sys.executable}" -c "print(123)"'
    tools = create_tools(db, workspace, allowed_test_commands=[command])

    result = tools.run_tests(command)

    assert result.passed is True
    assert result.exit_code == 0
    assert result.stdout.strip() == "123"
    assert db.scalar(select(ResultRecord)) is not None
    event = last_event(db).payload_json
    assert event["tool_name"] == "run_tests"
    assert event["success"] is True
    assert event["passed"] is True


def test_get_diff_returns_changed_files(db: Session, workspace: Path) -> None:
    init_git_repo(workspace)
    tools = create_tools(db, workspace)
    tools.write_file(
        "src/calculator.py",
        "def add(left, right):\n    return left + right + 1\n",
    )

    result = tools.get_diff()

    assert result.changed_files == ["src/calculator.py"]
    assert "diff --git a/src/calculator.py b/src/calculator.py" in result.patch_text
    assert last_event(db).payload_json["tool_name"] == "get_diff"


def test_submit_patch_persists_patch_and_logs_event(db: Session, workspace: Path) -> None:
    init_git_repo(workspace)
    agent_run_id = create_agent_run(db)
    tools = AgentWorkspaceTools(
        db=db,
        agent_run_id=agent_run_id,
        workspace_path=workspace,
        allowed_test_commands=["pytest"],
    )
    tools.write_file(
        "src/calculator.py",
        "def add(left, right):\n    return left + right + 1\n",
    )

    result = tools.submit_patch()

    assert result.changed_files == ["src/calculator.py"]
    assert "diff --git" in result.patch_text
    assert result.generated_patch_id is not None
    event = last_event(db).payload_json
    assert event["tool_name"] == "submit_patch"
    assert event["success"] is True


def create_tools(
    db: Session,
    workspace: Path,
    *,
    allowed_test_commands: list[str] | None = None,
) -> AgentWorkspaceTools:
    return AgentWorkspaceTools(
        db=db,
        agent_run_id=create_agent_run(db),
        workspace_path=workspace,
        allowed_test_commands=allowed_test_commands or ["pytest"],
        max_file_read_bytes=10_000,
        max_search_results=10,
    )


def create_agent_run(db: Session) -> UUID:
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
        issue_body="Calculator should work.",
        issue_comments=[],
        pull_request_number=43,
        base_commit="1111111111111111111111111111111111111111",
        setup_commands=[],
        test_commands=["pytest"],
        status="running",
    )
    db.add(task)
    db.flush()

    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="openai",
        model_name="placeholder-model",
        status="running",
    )
    db.add(run)
    db.commit()
    return run.id


def last_event(db: Session) -> AgentEvent:
    events = list(db.scalars(select(AgentEvent)).all())
    assert events
    return events[-1]


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
