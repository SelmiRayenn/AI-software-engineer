"""Add trusted hidden evaluation tests and metrics."""

import sqlalchemy as sa

from alembic import op

revision = "20260911_0007"
down_revision = "20260911_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hidden_eval_tests",
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("commands", sa.JSON(), nullable=False),
        sa.Column("files_payload", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_hidden_eval_tests_benchmark_task_id",
        "hidden_eval_tests",
        ["benchmark_task_id"],
        unique=False,
    )
    op.add_column(
        "evaluation_metrics",
        sa.Column("hidden_tests_passed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "evaluation_metrics",
        sa.Column(
            "hidden_tests_run_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "evaluation_metrics",
        sa.Column(
            "hidden_tests_failed_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluation_metrics", "hidden_tests_failed_count")
    op.drop_column("evaluation_metrics", "hidden_tests_run_count")
    op.drop_column("evaluation_metrics", "hidden_tests_passed")
    op.drop_index("ix_hidden_eval_tests_benchmark_task_id", table_name="hidden_eval_tests")
    op.drop_table("hidden_eval_tests")
