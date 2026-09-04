from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def make_alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(revision: str = "head") -> None:
    command.upgrade(make_alembic_config(), revision)


def create_migration(message: str) -> None:
    command.revision(make_alembic_config(), message=message, autogenerate=True)


def show_current_revision() -> None:
    config = make_alembic_config()
    command.current(config)
    command.heads(config)
