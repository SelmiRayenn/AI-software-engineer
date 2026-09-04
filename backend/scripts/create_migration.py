from __future__ import annotations

import argparse

from app.db.migrations import create_migration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a new Alembic migration.")
    parser.add_argument("message", help="Short migration message, for example 'add run labels'.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    create_migration(args.message)
