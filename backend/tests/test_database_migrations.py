from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app import models  # noqa: F401
from app.db.base import Base
from app.db.migrations import make_alembic_config

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

EXPECTED_TABLES = {
    "agent_events",
    "agent_runs",
    "benchmark_tasks",
    "evaluation_metrics",
    "generated_patches",
    "gold_patches",
    "human_reviews",
    "repositories",
    "test_results",
    "repository_indexes",
    "indexed_files",
    "indexed_symbols",
    "indexed_chunks",
    "chunk_embeddings",
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
