import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.test_phases import TEST_PHASE_BASELINE, TEST_PHASE_POST_PATCH, TEST_PHASE_SETUP
from app.db.base import Base
from app.models import AgentRun, AgentRunFailure, BenchmarkTask, GeneratedPatch, Repository
from app.models import TestResult as ResultRecord
from app.test_execution import TestExecutionSafetyError as ExecutionSafetyError
from app.test_execution import TestExecutionService as ExecutionService

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
    (root / "src" / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    init_git_repo(root)
    return root


def test_setup_command_result_storage(db: Session, workspace: Path) -> None:
    setup_command = python_command("print('setup ok')")
    run_id = create_agent_run(db, workspace, setup_commands=[setup_command])

    result = ExecutionService(db=db, agent_run_id=run_id).run_setup_commands()

    assert result.passed is True
    stored = list(db.scalars(select(ResultRecord).where(ResultRecord.agent_run_id == run_id)).all())
    assert len(stored) == 1
    assert stored[0].phase == TEST_PHASE_SETUP
    assert stored[0].command == setup_command
    assert stored[0].stdout.strip() == "setup ok"


def test_baseline_result_storage(db: Session, workspace: Path) -> None:
    test_command = python_command("print('baseline ok')")
    run_id = create_agent_run(db, workspace, test_commands=[test_command])

    result = ExecutionService(db=db, agent_run_id=run_id).run_baseline_tests()

    assert result.passed is True
    assert result.test_results[0].phase == TEST_PHASE_BASELINE
    assert result.test_results[0].stdout.strip() == "baseline ok"


def test_post_patch_result_storage_and_patch_application(
    db: Session,
    workspace: Path,
) -> None:
    test_command = python_command(
        "from pathlib import Path; "
        "text = Path('src/calculator.py').read_text(); "
        "assert 'a + b + 1' in text; "
        "print('post patch ok')"
    )
    run_id = create_agent_run(db, workspace, test_commands=[test_command])
    db.add(
        GeneratedPatch(
            agent_run_id=run_id,
            patch_text=calculator_patch(),
            changed_files=["src/calculator.py"],
        )
    )
    db.commit()

    result = ExecutionService(db=db, agent_run_id=run_id).run_post_patch_tests()

    assert result.passed is True
    assert result.patch_status == "applied"
    assert result.test_results[0].phase == TEST_PHASE_POST_PATCH
    assert result.test_results[0].stdout.strip() == "post patch ok"
    assert db.get(AgentRun, run_id).status == "completed"


def test_failed_command_is_captured(db: Session, workspace: Path) -> None:
    failing_command = python_command(
        "import sys; print('bad stdout'); sys.stderr.write('bad stderr'); sys.exit(7)"
    )
    run_id = create_agent_run(db, workspace, test_commands=[failing_command])

    result = ExecutionService(db=db, agent_run_id=run_id).run_baseline_tests()

    assert result.passed is False
    stored = result.test_results[0]
    assert stored.passed is False
    assert stored.exit_code == 7
    assert stored.stdout.strip() == "bad stdout"
    assert stored.stderr == "bad stderr"


def test_output_capture_is_limited(db: Session, workspace: Path) -> None:
    noisy_command = python_command("print('x' * 120)")
    run_id = create_agent_run(db, workspace, test_commands=[noisy_command])

    result = ExecutionService(
        db=db,
        agent_run_id=run_id,
        max_log_bytes=20,
    ).run_baseline_tests()

    assert result.test_results[0].stdout.startswith("[truncated to last 20 bytes]")
    assert result.test_results[0].stdout.endswith("x" * 19 + "\n")


def test_command_timeout_is_captured(db: Session, workspace: Path) -> None:
    slow_command = python_command("import time; time.sleep(2)")
    run_id = create_agent_run(db, workspace, test_commands=[slow_command])

    result = ExecutionService(
        db=db,
        agent_run_id=run_id,
        command_timeout_seconds=1,
    ).run_baseline_tests()

    assert result.passed is False
    assert result.test_results[0].exit_code == 124
    assert "exceeded timeout" in result.test_results[0].stderr


def test_disallowed_command_is_rejected(db: Session, workspace: Path) -> None:
    allowed_command = python_command("print('allowed')")
    run_id = create_agent_run(db, workspace, test_commands=[allowed_command])

    with pytest.raises(ExecutionSafetyError):
        ExecutionService(db=db, agent_run_id=run_id).run_test_command(
            phase=TEST_PHASE_BASELINE,
            command="pytest",
        )


def test_setup_failure_marks_run_failed(db: Session, workspace: Path) -> None:
    setup_command = python_command("import sys; sys.stderr.write('setup failed'); sys.exit(2)")
    run_id = create_agent_run(db, workspace, setup_commands=[setup_command])

    result = ExecutionService(db=db, agent_run_id=run_id).run_setup_commands()

    assert result.passed is False
    run = db.get(AgentRun, run_id)
    assert run.status == "failed"
    assert run.completed_at is not None
    stored = result.setup_results[0]
    assert stored.phase == TEST_PHASE_SETUP
    assert stored.exit_code == 2
    assert stored.stderr == "setup failed"
    failure = db.scalar(select(AgentRunFailure).where(AgentRunFailure.agent_run_id == run_id))
    assert failure.category == "setup_failed"
    assert failure.source_event_id is not None


def test_post_patch_failure_marks_run_failed(db: Session, workspace: Path) -> None:
    failing_command = python_command("import sys; sys.exit(3)")
    run_id = create_agent_run(db, workspace, test_commands=[failing_command])

    result = ExecutionService(db=db, agent_run_id=run_id).run_post_patch_tests()

    assert result.passed is False
    run = db.get(AgentRun, run_id)
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.failure.category == "post_patch_tests_failed"


def test_setup_timeout_is_classified(db: Session, workspace: Path) -> None:
    setup_command = python_command("import time; time.sleep(2)")
    run_id = create_agent_run(db, workspace, setup_commands=[setup_command])

    result = ExecutionService(
        db=db,
        agent_run_id=run_id,
        command_timeout_seconds=1,
    ).run_setup_commands()

    assert result.passed is False
    assert db.get(AgentRun, run_id).failure.category == "timeout"


def create_agent_run(
    db: Session,
    workspace: Path,
    *,
    setup_commands: list[str] | None = None,
    test_commands: list[str] | None = None,
    status: str = "queued",
) -> UUID:
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
        setup_commands=setup_commands or [],
        test_commands=test_commands or [python_command("print('ok')")],
        status="ready",
    )
    db.add(task)
    db.flush()

    run = AgentRun(
        benchmark_task_id=task.id,
        model_provider="mock",
        model_name="scripted-mock",
        status=status,
        workspace_id="test-workspace",
        workspace_path=str(workspace),
    )
    db.add(run)
    db.commit()
    return run.id


def python_command(code: str) -> str:
    escaped = code.replace('"', '\\"')
    return f'"{sys.executable}" -c "{escaped}"'


def calculator_patch() -> str:
    return (
        "diff --git a/src/calculator.py b/src/calculator.py\n"
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b + 1\n"
    )


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
