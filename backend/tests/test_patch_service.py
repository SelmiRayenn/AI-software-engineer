import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AgentEvent, AgentRun, BenchmarkTask, GeneratedPatch, PatchQuality, Repository
from app.patch_quality import PatchQualityService
from app.patches import PatchApplyError, PatchSafetyError, PatchService

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


def test_diff_generation_reports_changed_files(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    (workspace / "src" / "calculator.py").write_text(
        "def add(a, b):\n    return a + b + 1\n",
        encoding="utf-8",
    )

    result = PatchService(db=db, agent_run_id=run_id).get_current_workspace_diff()

    assert result.changed_files == ["src/calculator.py"]
    assert "diff --git a/src/calculator.py b/src/calculator.py" in result.patch_text
    assert result.stats.changed_files_count == 1
    assert result.stats.additions == 1
    assert result.stats.deletions == 1


def test_diff_generation_includes_untracked_text_files(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    (workspace / "src" / "new_feature.py").write_text("ENABLED = True\n", encoding="utf-8")

    result = PatchService(db=db, agent_run_id=run_id).get_current_workspace_diff()

    assert result.changed_files == ["src/new_feature.py"]
    assert "new file mode 100644" in result.patch_text
    assert "+++ b/src/new_feature.py" in result.patch_text


def test_patch_application_success_creates_generated_patch(
    db: Session,
    workspace: Path,
) -> None:
    run_id = create_agent_run(db, workspace)

    result = PatchService(db=db, agent_run_id=run_id).apply_unified_diff(calculator_patch())

    assert result.changed_files == ["src/calculator.py"]
    assert "return a + b + 1" in (workspace / "src" / "calculator.py").read_text(encoding="utf-8")
    stored_patch = db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == run_id))
    assert stored_patch is not None
    assert stored_patch.patch_text == calculator_patch()
    assert stored_patch.changed_files == ["src/calculator.py"]
    quality = db.scalar(
        select(PatchQuality).where(PatchQuality.generated_patch_id == stored_patch.id)
    )
    assert quality is not None
    assert quality.total_changed_lines == 2
    event_types = [event.event_type for event in db.scalars(select(AgentEvent)).all()]
    assert "generated_patch_stored" in event_types
    assert "patch_applied" in event_types


def test_invalid_patch_is_rejected(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)

    with pytest.raises(PatchApplyError):
        PatchService(db=db, agent_run_id=run_id).apply_unified_diff(
            "diff --git a/src/calculator.py b/src/calculator.py\n"
            "--- a/src/calculator.py\n"
            "+++ b/src/calculator.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return missing\n"
            "+    return a + b + 1\n"
        )

    assert db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == run_id)) is None


def test_path_traversal_patch_is_rejected(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    patch_text = (
        "diff --git a/../secret.txt b/../secret.txt\n"
        "--- a/../secret.txt\n"
        "+++ b/../secret.txt\n"
        "@@ -0,0 +1 @@\n"
        "+secret\n"
    )

    with pytest.raises(PatchSafetyError):
        PatchService(db=db, agent_run_id=run_id).list_changed_files_from_patch(patch_text)


def test_hidden_gold_patch_path_is_rejected(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    patch_text = (
        "diff --git a/.benchmark_gold/gold_patch.diff b/.benchmark_gold/gold_patch.diff\n"
        "--- a/.benchmark_gold/gold_patch.diff\n"
        "+++ b/.benchmark_gold/gold_patch.diff\n"
        "@@ -0,0 +1 @@\n"
        "+hidden\n"
    )

    with pytest.raises(PatchSafetyError):
        PatchService(db=db, agent_run_id=run_id).list_changed_files_from_patch(patch_text)


def test_binary_patch_is_rejected(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    patch_text = "diff --git a/image.png b/image.png\nGIT binary patch\nliteral 4\nabcd\n"

    with pytest.raises(PatchSafetyError):
        PatchService(db=db, agent_run_id=run_id).list_changed_files_from_patch(patch_text)


def test_changed_file_extraction(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)

    changed_files = PatchService(db=db, agent_run_id=run_id).list_changed_files_from_patch(
        calculator_patch()
    )

    assert changed_files == ["src/calculator.py"]


def test_patch_size_limit_is_enforced(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    service = PatchService(db=db, agent_run_id=run_id, max_patch_bytes=50)

    with pytest.raises(PatchSafetyError):
        service.list_changed_files_from_patch(calculator_patch())


def test_changed_file_count_limit_is_enforced(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace)
    service = PatchService(db=db, agent_run_id=run_id, max_changed_files=1)
    patch_text = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -0,0 +1 @@\n"
        "+a\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -0,0 +1 @@\n"
        "+b\n"
    )

    with pytest.raises(PatchSafetyError):
        service.list_changed_files_from_patch(patch_text)


def test_quality_limit_rejects_patch_before_workspace_mutation(
    db: Session,
    workspace: Path,
) -> None:
    run_id = create_agent_run(db, workspace)
    service = PatchService(
        db=db,
        agent_run_id=run_id,
        quality_service=PatchQualityService(db, max_patch_changed_lines=1),
    )

    with pytest.raises(PatchSafetyError, match="quality guardrails"):
        service.apply_unified_diff(calculator_patch())

    assert "return a + b\n" in (workspace / "src" / "calculator.py").read_text(encoding="utf-8")
    assert db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == run_id)) is None
    events = db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run_id)).all()
    assert [event.event_type for event in events] == ["patch_quality_rejected"]


def test_patch_application_rejects_completed_runs(db: Session, workspace: Path) -> None:
    run_id = create_agent_run(db, workspace, status="completed")

    with pytest.raises(PatchSafetyError):
        PatchService(db=db, agent_run_id=run_id).apply_unified_diff(calculator_patch())


def create_agent_run(db: Session, workspace: Path, *, status: str = "running") -> UUID:
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
