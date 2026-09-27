from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    GeneratedPatch,
    GoldPatch,
    PatchQuality,
    Repository,
)
from app.patch_quality import PatchQualityRejectedError, PatchQualityService

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
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
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_small_valid_patch_is_analyzed(db: Session) -> None:
    task = create_task(db, gold_files=["src/calculator.py"])

    result = PatchQualityService(db).analyze_patch(
        patch_text=patch_for("src/calculator.py"),
        changed_files=["src/calculator.py"],
        benchmark_task=task,
    )

    assert result.accepted
    assert result.changed_file_count == 1
    assert result.added_lines == 1
    assert result.removed_lines == 1
    assert result.total_changed_lines == 2
    assert result.changed_source_files == ["src/calculator.py"]
    assert result.unrelated_files == []
    assert result.changed_hunk_count == 1
    assert result.added_removed_ratio == 1.0
    assert result.file_kind_counts == {
        "source": 1,
        "test": 0,
        "docs": 0,
        "config": 0,
        "other": 0,
    }
    assert result.minimization_score >= 0.95
    assert result.minimization_warnings == []


def test_changed_files_are_classified_by_kind(db: Session) -> None:
    task = create_task(db)
    paths = [
        "docs/usage.md",
        "settings.yaml",
        "src/calculator.py",
        "tests/test_calculator.py",
    ]

    result = PatchQualityService(db).analyze_patch(
        patch_text="".join(patch_for(path) for path in paths),
        changed_files=paths,
        benchmark_task=task,
    )

    assert result.changed_source_files == ["src/calculator.py"]
    assert result.changed_test_files == ["tests/test_calculator.py"]
    assert result.changed_docs_config_files == ["docs/usage.md", "settings.yaml"]
    assert result.file_kind_counts == {
        "source": 1,
        "test": 1,
        "docs": 1,
        "config": 1,
        "other": 0,
    }


def test_too_many_files_is_rejected(db: Session) -> None:
    task = create_task(db)
    service = PatchQualityService(db, max_patch_files=1)

    result = service.analyze_patch(
        patch_text=patch_for("src/a.py") + patch_for("src/b.py"),
        changed_files=["src/a.py", "src/b.py"],
        benchmark_task=task,
    )

    with pytest.raises(PatchQualityRejectedError, match="maximum is 1"):
        service.enforce(result)


def test_too_many_changed_lines_is_rejected(db: Session) -> None:
    task = create_task(db)
    service = PatchQualityService(db, max_patch_changed_lines=1)

    result = service.analyze_patch(
        patch_text=patch_for("src/calculator.py"),
        changed_files=["src/calculator.py"],
        benchmark_task=task,
    )

    with pytest.raises(PatchQualityRejectedError, match="changes 2 lines"):
        service.enforce(result)


@pytest.mark.parametrize("path", ["dist/app.js", "src/__pycache__/module.pyc"])
def test_generated_or_cache_file_is_blocked(db: Session, path: str) -> None:
    task = create_task(db)
    service = PatchQualityService(db)

    result = service.analyze_patch(
        patch_text=patch_for(path),
        changed_files=[path],
        benchmark_task=task,
    )

    assert result.suspicious_generated_files == [path]
    with pytest.raises(PatchQualityRejectedError, match="generated, cache, or build-output"):
        service.enforce(result)


def test_lockfile_is_blocked_without_task_allowance(db: Session) -> None:
    task = create_task(db)
    service = PatchQualityService(db)

    result = service.analyze_patch(
        patch_text=patch_for("package-lock.json"),
        changed_files=["package-lock.json"],
        benchmark_task=task,
    )

    assert result.lockfiles == ["package-lock.json"]
    with pytest.raises(PatchQualityRejectedError, match="Lockfile changes"):
        service.enforce(result)


def test_dependency_file_is_blocked_without_task_allowance(db: Session) -> None:
    task = create_task(db)
    service = PatchQualityService(db)

    result = service.analyze_patch(
        patch_text=patch_for("pyproject.toml"),
        changed_files=["pyproject.toml"],
        benchmark_task=task,
    )

    assert result.dependency_files == ["pyproject.toml"]
    with pytest.raises(PatchQualityRejectedError, match="Dependency file changes"):
        service.enforce(result)


def test_explicit_task_allowance_warns_but_allows_dependency_changes(db: Session) -> None:
    task = create_task(db, allow_dependency_file_changes=True)
    service = PatchQualityService(db)

    result = service.analyze_patch(
        patch_text=patch_for("pyproject.toml"),
        changed_files=["pyproject.toml"],
        benchmark_task=task,
    )

    assert result.accepted
    assert "Patch changes dependency files under an explicit task allowance." in result.warnings


def test_whitespace_only_patch_is_detected(db: Session) -> None:
    task = create_task(db)
    patch_text = patch_for("src/calculator.py", before="value=1", after="value = 1")

    result = PatchQualityService(db).analyze_patch(
        patch_text=patch_text,
        changed_files=["src/calculator.py"],
        benchmark_task=task,
    )

    assert result.whitespace_only
    assert "Patch only changes formatting or whitespace." in result.warnings
    assert result.formatting_only_hunk_count == 1
    assert result.unrelated_formatting_hunk_count == 1
    assert "formatting_only_change" in result.minimization_warnings


def test_broad_patch_gets_low_minimization_score(db: Session) -> None:
    task = create_task(db)
    paths = [f"src/module_{index}.py" for index in range(10)]

    result = PatchQualityService(db).analyze_patch(
        patch_text="".join(patch_for(path) for path in paths),
        changed_files=paths,
        benchmark_task=task,
    )

    assert result.changed_file_count == 10
    assert result.changed_hunk_count == 10
    assert result.minimization_score <= 0.6
    assert "broad_patch" in result.minimization_warnings
    assert result.minimization_penalties["broad_patch"] == 0.25


def test_large_contiguous_rewrite_is_warned(db: Session) -> None:
    task = create_task(db)
    patch_text = (
        "diff --git a/src/rewrite.py b/src/rewrite.py\n"
        "--- a/src/rewrite.py\n"
        "+++ b/src/rewrite.py\n"
        "@@ -1,50 +1,50 @@\n"
        + "".join(f"-old_{index}\n" for index in range(50))
        + "".join(f"+new_{index}\n" for index in range(50))
    )

    result = PatchQualityService(db).analyze_patch(
        patch_text=patch_text,
        changed_files=["src/rewrite.py"],
        benchmark_task=task,
    )

    assert result.large_rewrite_hunk_count == 1
    assert "large_rewrite" in result.minimization_warnings


def test_generated_looking_block_is_warned(db: Session) -> None:
    task = create_task(db)
    generated = "# AUTO-GENERATED - DO NOT EDIT\n" + "\n".join(
        f"VALUE_{index} = {index}" for index in range(25)
    )

    result = PatchQualityService(db).analyze_patch(
        patch_text=patch_for("src/generated_constants.py", before="old = 1", after=generated),
        changed_files=["src/generated_constants.py"],
        benchmark_task=task,
    )

    assert result.generated_block_count == 1
    assert "generated_block_suspected" in result.minimization_warnings


def test_duplicate_edits_are_counted(db: Session) -> None:
    task = create_task(db)
    repeated_change = patch_for("src/a.py") + patch_for("src/b.py")

    result = PatchQualityService(db).analyze_patch(
        patch_text=repeated_change,
        changed_files=["src/a.py", "src/b.py"],
        benchmark_task=task,
    )

    assert result.duplicate_edit_count >= 1
    assert result.minimization_penalties["duplicate_edits"] > 0


@pytest.mark.parametrize(
    ("paths", "warning"),
    [
        ([f"tests/test_case_{index}.py" for index in range(3)], "excessive_test_changes"),
        ([f"config/settings_{index}.yaml" for index in range(3)], "excessive_config_changes"),
    ],
)
def test_excessive_file_kind_changes_are_warned(
    db: Session,
    paths: list[str],
    warning: str,
) -> None:
    task = create_task(db)

    result = PatchQualityService(db).analyze_patch(
        patch_text="".join(patch_for(path) for path in paths),
        changed_files=paths,
        benchmark_task=task,
    )

    assert warning in result.minimization_warnings
    assert result.minimization_penalties[warning] == 0.1


def test_modified_file_without_pre_edit_evidence_is_warned(db: Session) -> None:
    task = create_task(db)
    patch = create_patch(db, task.id, patch_for("src/unseen.py"), ["src/unseen.py"])
    db.add(
        AgentEvent(
            agent_run_id=patch.agent_run_id,
            event_type="agent_tool_call",
            payload_json={
                "tool_name": "read_file",
                "success": True,
                "files_read": ["src/seen.py"],
                "files_modified": [],
            },
        )
    )
    db.commit()

    quality = PatchQualityService(db).get_or_create(patch.id)

    assert quality.uninspected_files == ["src/unseen.py"]
    assert "uninspected_file_modified" in quality.minimization_warnings
    assert quality.minimization_penalties["uninspected_files"] == 0.05


def test_candidate_ranked_file_counts_as_pre_edit_evidence(db: Session) -> None:
    task = create_task(db)
    patch = create_patch(db, task.id, patch_for("src/ranked.py"), ["src/ranked.py"])
    db.add(
        AgentEvent(
            agent_run_id=patch.agent_run_id,
            event_type="candidate_files_submitted",
            payload_json={
                "ranked_files": [
                    {"path": "src/ranked.py", "reason": "Relevant", "confidence": "high"}
                ]
            },
        )
    )
    db.commit()

    quality = PatchQualityService(db).get_or_create(patch.id)

    assert quality.uninspected_files == []
    assert "uninspected_file_modified" not in quality.minimization_warnings


def test_quality_metrics_are_stored_and_warnings_exposed(
    client: TestClient,
    db: Session,
) -> None:
    task = create_task(db, gold_files=["src/expected.py"])
    patch = create_patch(db, task.id, patch_for("src/other.py"), ["src/other.py"])

    response = client.get(f"/patches/{patch.id}/quality")

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is False
    assert payload["unrelated_files_count"] == 1
    assert payload["max_patch_files"] == 20
    assert payload["max_patch_changed_lines"] == 1000
    assert payload["minimization_score"] == pytest.approx(0.95)
    assert payload["minimization_warnings"] == ["uninspected_file_modified"]
    assert payload["uninspected_files"] == ["src/other.py"]
    assert payload["warnings"] == [
        "Patch changes 1 file(s) outside the gold patch file set.",
        "Patch modifies files not inspected or candidate-ranked before editing.",
    ]
    assert payload["hard_limit_violations"] == [
        "Source files must be candidate-ranked before editing: src/other.py."
    ]
    with TestingSessionLocal() as session:
        stored = session.scalar(
            select(PatchQuality).where(PatchQuality.generated_patch_id == patch.id)
        )
        assert stored is not None
        assert stored.total_changed_lines == 2
        assert stored.unrelated_files == ["src/other.py"]
        assert stored.minimization_version == 1
        assert stored.changed_hunk_count == 1


def test_missing_patch_quality_returns_404(client: TestClient) -> None:
    response = client.get("/patches/11111111-1111-1111-1111-111111111111/quality")

    assert response.status_code == 404
    assert response.json()["detail"] == "Generated patch not found."


def create_task(
    db: Session,
    *,
    gold_files: list[str] | None = None,
    allow_dependency_file_changes: bool = False,
) -> BenchmarkTask:
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
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=["pytest"],
        allow_dependency_file_changes=allow_dependency_file_changes,
        status="ready",
    )
    db.add(task)
    db.flush()
    if gold_files is not None:
        db.add(
            GoldPatch(
                benchmark_task_id=task.id,
                changed_files=gold_files,
                patch_text="hidden",
                test_files=[],
            )
        )
    db.commit()
    db.refresh(task)
    return task


def create_patch(
    db: Session,
    task_id: UUID,
    patch_text: str,
    changed_files: list[str],
) -> GeneratedPatch:
    run = AgentRun(
        benchmark_task_id=task_id,
        model_provider="mock",
        model_name="mock",
        status="completed",
    )
    db.add(run)
    db.flush()
    patch = GeneratedPatch(
        agent_run_id=run.id,
        patch_text=patch_text,
        changed_files=changed_files,
        version=1,
        is_selected=True,
    )
    db.add(patch)
    db.commit()
    db.refresh(patch)
    return patch


def patch_for(path: str, *, before: str = "old", after: str = "new") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )
