"""Add normalized difficulty and tags to benchmark tasks."""

import sqlalchemy as sa

from alembic import op

revision = "20260920_0012"
down_revision = "20260920_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_tasks",
        sa.Column("difficulty", sa.String(20), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "benchmark_tasks",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.create_index("ix_benchmark_tasks_difficulty", "benchmark_tasks", ["difficulty"])


def downgrade() -> None:
    op.drop_index("ix_benchmark_tasks_difficulty", table_name="benchmark_tasks")
    op.drop_column("benchmark_tasks", "tags")
    op.drop_column("benchmark_tasks", "difficulty")
