from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from alembic import command
from app import models
from app.db.base import Base
from app.db.migrations import make_alembic_config

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

EXPECTED_TABLES = {
    "agent_events",
    "agent_runs",
    "agent_run_failures",
    "benchmark_tasks",
    "benchmark_imports",
    "benchmark_packs",
    "benchmark_pack_tasks",
    "benchmark_pack_runs",
    "benchmark_pack_run_tasks",
    "evaluation_metrics",
    "generated_patches",
    "gold_patches",
    "human_reviews",
    "hidden_eval_tests",
    "repositories",
    "test_results",
    "repository_indexes",
    "indexed_files",
    "indexed_symbols",
    "indexed_chunks",
    "chunk_embeddings",
    "patch_qualities",
    "flakiness_checks",
    "flakiness_check_runs",
}


def test_alembic_config_imports() -> None:
    config = make_alembic_config("sqlite:///:memory:")

    assert isinstance(config, Config)
    assert config.get_main_option("script_location") == str(BACKEND_ROOT / "alembic")
    assert config.get_main_option("sqlalchemy.url") == "sqlite:///:memory:"


def test_sqlalchemy_metadata_can_be_loaded() -> None:
    assert EXPECTED_TABLES.issubset(Base.metadata.tables.keys())
    assert "pull_request_number" in Base.metadata.tables["benchmark_tasks"].columns
    assert "workspace_id" in Base.metadata.tables["agent_runs"].columns
    assert "workspace_path" in Base.metadata.tables["agent_runs"].columns
    assert "allow_lockfile_changes" in Base.metadata.tables["benchmark_tasks"].columns
    assert "allow_dependency_file_changes" in Base.metadata.tables["benchmark_tasks"].columns
    assert {"difficulty", "tags"}.issubset(Base.metadata.tables["benchmark_tasks"].columns.keys())
    assert {
        "repetitions_requested",
        "inconsistent_results",
        "average_duration_seconds",
        "workspace_retained",
    }.issubset(Base.metadata.tables["flakiness_checks"].columns.keys())
    assert {
        "baseline_tests_passed",
        "post_patch_tests_passed",
        "hidden_tests_passed",
        "hidden_tests_run_count",
        "hidden_tests_failed_count",
        "issue_resolved",
        "regression_detected",
        "issue_specific_score",
    }.issubset(Base.metadata.tables["evaluation_metrics"].columns.keys())


def test_pack_run_migration_preserves_existing_packs_and_roundtrips(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'pack-runs.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260920_0010")
    engine = create_engine(database_url)
    with Session(engine) as db:
        pack = models.BenchmarkPack(name="Existing pack", slug="existing", version="1")
        db.add(pack)
        db.commit()
        pack_id = pack.id

    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert {"benchmark_pack_runs", "benchmark_pack_run_tasks"}.issubset(inspector.get_table_names())
    with Session(engine) as db:
        assert db.get(models.BenchmarkPack, pack_id).name == "Existing pack"
        repository = models.Repository(
            name="sample", owner="example", url="https://github.com/example/sample"
        )
        task = models.BenchmarkTask(repository=repository, issue_title="Fix", base_commit="base")
        agent_run = models.AgentRun(
            benchmark_task=task, model_provider="mock", model_name="mock-model"
        )
        db.add(agent_run)
        db.flush()
        pack_run = models.BenchmarkPackRun(
            benchmark_pack_id=pack_id,
            pack_name="Existing pack",
            pack_slug="existing",
            pack_version="1",
            model_provider="mock",
            model_name="mock-model",
            run_config={},
            started_at=datetime.now(UTC),
        )
        entry = models.BenchmarkPackRunTask(
            pack_run=pack_run,
            benchmark_task_id=task.id,
            agent_run_id=agent_run.id,
            order_index=0,
            task_snapshot={},
            definition_hash="a" * 64,
        )
        db.add(entry)
        db.commit()
        assert entry.created_at and entry.status == "queued"
        assert pack_run.include_hidden_tests is False and pack_run.stop_on_task_failure is False
        assert len(pack_run.tasks) == 1

    command.downgrade(config, "20260920_0010")
    assert "benchmark_pack_runs" not in inspect(engine).get_table_names()
    assert "benchmark_pack_run_tasks" not in inspect(engine).get_table_names()
    with Session(engine) as db:
        assert db.get(models.BenchmarkPack, pack_id).slug == "existing"
    engine.dispose()


def test_flakiness_migration_upgrades_and_downgrades_task_metadata(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'flakiness.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260920_0012")
    engine = create_engine(database_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert {"flakiness_checks", "flakiness_check_runs"}.issubset(inspector.get_table_names())
    assert {"status", "pass_count", "fail_count", "setup_results"}.issubset(
        _column_names(inspector, "flakiness_checks")
    )
    command.downgrade(config, "20260920_0012")
    assert "flakiness_checks" not in inspect(engine).get_table_names()
    assert "difficulty" in _column_names(inspect(engine), "benchmark_tasks")
    engine.dispose()


def test_migration_command_documentation_is_accurate() -> None:
    docs = (REPO_ROOT / "docs" / "database-migrations.md").read_text(encoding="utf-8")
    backend_readme = (BACKEND_ROOT / "README.md").read_text(encoding="utf-8")
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for command_text in (
        "python scripts/apply_migrations.py",
        "python scripts/create_migration.py",
        "python scripts/migration_status.py",
        "alembic upgrade head",
        "alembic revision --autogenerate",
        "alembic current",
        "alembic heads",
    ):
        assert command_text in docs

    assert "python scripts/apply_migrations.py" in backend_readme
    assert "python scripts/apply_migrations.py" in root_readme


def test_initial_migration_upgrades_empty_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-smoke.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = make_alembic_config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert EXPECTED_TABLES.issubset(tables)
    assert "alembic_version" in tables
    assert "pull_request_number" in _column_names(inspector, "benchmark_tasks")
    assert "workspace_id" in _column_names(inspector, "agent_runs")
    assert "workspace_path" in _column_names(inspector, "agent_runs")
    assert {"difficulty", "tags"}.issubset(_column_names(inspector, "benchmark_tasks"))
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()


def test_repository_index_migration_upgrades_and_downgrades_existing_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'index-migration.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260904_0001")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO repositories (id, owner, name, url, default_branch) "
            "VALUES ('11111111111111111111111111111111', 'example', 'repo', "
            "'https://github.com/example/repo', 'main')"
        )
    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert {"repository_indexes", "indexed_files", "indexed_symbols"}.issubset(
        inspector.get_table_names()
    )
    assert "checksum" in _column_names(inspector, "indexed_files")
    assert "line_number" in _column_names(inspector, "indexed_symbols")
    command.downgrade(config, "20260904_0001")
    assert "repository_indexes" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM repositories").scalar_one() == "repo"
    engine.dispose()


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def test_embedding_migration_upgrades_and_downgrades_index_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'embeddings-migration.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260905_0002")
    engine = create_engine(database_url)
    command.upgrade(config, "head")
    assert {"indexed_chunks", "chunk_embeddings"}.issubset(inspect(engine).get_table_names())
    assert {"dimensions", "vector", "input_checksum"}.issubset(
        _column_names(inspect(engine), "chunk_embeddings")
    )
    command.downgrade(config, "20260905_0002")
    assert "indexed_files" in inspect(engine).get_table_names()
    assert "chunk_embeddings" not in inspect(engine).get_table_names()
    assert "indexed_chunks" not in inspect(engine).get_table_names()
    engine.dispose()


def test_patch_quality_migration_upgrades_and_downgrades_current_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'patch-quality-migration.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260909_0004")
    engine = create_engine(database_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "patch_qualities" in inspector.get_table_names()
    assert {
        "changed_file_count",
        "total_changed_lines",
        "max_patch_files",
        "max_patch_changed_lines",
        "warnings",
        "hard_limit_violations",
    }.issubset(_column_names(inspector, "patch_qualities"))
    assert {
        "allow_lockfile_changes",
        "allow_dependency_file_changes",
    }.issubset(_column_names(inspector, "benchmark_tasks"))

    command.downgrade(config, "20260909_0004")

    inspector = inspect(engine)
    assert "patch_qualities" not in inspector.get_table_names()
    assert "allow_lockfile_changes" not in _column_names(inspector, "benchmark_tasks")
    engine.dispose()


def test_run_failure_migration_upgrades_and_downgrades_current_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'run-failure-migration.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260911_0005")
    engine = create_engine(database_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "agent_run_failures" in inspector.get_table_names()
    assert {
        "agent_run_id",
        "category",
        "human_readable_summary",
        "source_event_id",
    }.issubset(_column_names(inspector, "agent_run_failures"))

    command.downgrade(config, "20260911_0005")

    assert "agent_run_failures" not in inspect(engine).get_table_names()
    engine.dispose()


def test_hidden_eval_migration_upgrades_and_downgrades_current_schema(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'hidden-eval-migration.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260911_0006")
    engine = create_engine(database_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "hidden_eval_tests" in inspector.get_table_names()
    assert {"benchmark_task_id", "commands", "files_payload", "enabled"}.issubset(
        _column_names(inspector, "hidden_eval_tests")
    )
    assert {"hidden_tests_passed", "hidden_tests_run_count", "hidden_tests_failed_count"}.issubset(
        _column_names(inspector, "evaluation_metrics")
    )

    with Session(engine) as db:
        repository = models.Repository(
            name="sample", owner="example", url="https://github.com/example/sample"
        )
        task = models.BenchmarkTask(
            repository=repository, issue_number=1, issue_title="Fix bug", base_commit="base"
        )
        suite = models.HiddenEvalTest(benchmark_task=task, name="regression", commands=["pytest"])
        db.add(suite)
        db.commit()
        assert suite.created_at is not None
        assert suite.enabled is True

    command.downgrade(config, "20260911_0006")

    assert "hidden_eval_tests" not in inspect(engine).get_table_names()
    assert "hidden_tests_passed" not in _column_names(inspect(engine), "evaluation_metrics")
    engine.dispose()


def test_issue_success_metrics_migration_upgrades_and_downgrades_current_schema(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'issue-success-metrics.db').as_posix()}"
    config = make_alembic_config(database_url)
    command.upgrade(config, "20260911_0007")
    engine = create_engine(database_url)

    command.upgrade(config, "head")

    columns = _column_names(inspect(engine), "evaluation_metrics")
    assert {
        "baseline_tests_passed",
        "post_patch_tests_passed",
        "issue_resolved",
        "regression_detected",
        "issue_specific_score",
    }.issubset(columns)

    command.downgrade(config, "20260911_0007")

    columns = _column_names(inspect(engine), "evaluation_metrics")
    assert "baseline_tests_passed" not in columns
    assert "issue_specific_score" not in columns
    assert "hidden_tests_passed" in columns
    engine.dispose()
