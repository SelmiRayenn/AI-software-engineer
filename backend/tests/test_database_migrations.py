from __future__ import annotations

from pathlib import Path

from alembic.config import Config
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


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}
