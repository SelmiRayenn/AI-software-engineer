"""Persist benchmark task flakiness checks and repetition results."""

import sqlalchemy as sa

from alembic import op

revision = "20260920_0013"
down_revision = "20260920_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flakiness_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("repetitions_requested", sa.Integer(), nullable=False),
        sa.Column("repetitions_completed", sa.Integer(), nullable=False),
        sa.Column("pass_count", sa.Integer(), nullable=False),
        sa.Column("fail_count", sa.Integer(), nullable=False),
        sa.Column("inconsistent_results", sa.Boolean(), nullable=False),
        sa.Column("average_duration_seconds", sa.Float(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("stop_on_first_failure", sa.Boolean(), nullable=False),
        sa.Column("command_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("workspace_retained", sa.Boolean(), nullable=False),
        sa.Column("setup_results", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_flakiness_checks_benchmark_task_id", "flakiness_checks", ["benchmark_task_id"]
    )
    op.create_index("ix_flakiness_checks_status", "flakiness_checks", ["status"])
    op.create_table(
        "flakiness_check_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("flakiness_check_id", sa.Uuid(), nullable=False),
        sa.Column("repetition_number", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("command_results", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["flakiness_check_id"], ["flakiness_checks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "flakiness_check_id", "repetition_number", name="uq_flakiness_check_repetition"
        ),
    )
    op.create_index(
        "ix_flakiness_check_runs_flakiness_check_id",
        "flakiness_check_runs",
        ["flakiness_check_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_flakiness_check_runs_flakiness_check_id", table_name="flakiness_check_runs"
    )
    op.drop_table("flakiness_check_runs")
    op.drop_index("ix_flakiness_checks_status", table_name="flakiness_checks")
    op.drop_index("ix_flakiness_checks_benchmark_task_id", table_name="flakiness_checks")
    op.drop_table("flakiness_checks")
