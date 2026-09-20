"""Persist benchmark pack run rosters and result snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "20260920_0011"
down_revision = "20260920_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_pack_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("benchmark_pack_id", sa.Uuid(), nullable=False),
        sa.Column("pack_name", sa.String(255), nullable=False),
        sa.Column("pack_slug", sa.String(255), nullable=False),
        sa.Column("pack_version", sa.String(100), nullable=False),
        sa.Column("model_provider", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("run_config", sa.JSON(), nullable=False),
        sa.Column("include_hidden_tests", sa.Boolean(), nullable=False),
        sa.Column("stop_on_task_failure", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("aggregates", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["benchmark_pack_id"], ["benchmark_packs.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_benchmark_pack_runs_benchmark_pack_id", "benchmark_pack_runs", ["benchmark_pack_id"]
    )
    op.create_index("ix_benchmark_pack_runs_status", "benchmark_pack_runs", ["status"])
    op.create_table(
        "benchmark_pack_run_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("benchmark_pack_run_id", sa.Uuid(), nullable=False),
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("task_snapshot", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("metric_summary", sa.JSON(), nullable=True),
        sa.Column("failure_category", sa.String(100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["benchmark_pack_run_id"], ["benchmark_pack_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("agent_run_id"),
        sa.UniqueConstraint(
            "benchmark_pack_run_id", "benchmark_task_id", name="uq_pack_run_tasks_task"
        ),
        sa.UniqueConstraint("benchmark_pack_run_id", "order_index", name="uq_pack_run_tasks_order"),
    )
    op.create_index(
        "ix_benchmark_pack_run_tasks_benchmark_pack_run_id",
        "benchmark_pack_run_tasks",
        ["benchmark_pack_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_benchmark_pack_run_tasks_benchmark_pack_run_id", table_name="benchmark_pack_run_tasks"
    )
    op.drop_table("benchmark_pack_run_tasks")
    op.drop_index("ix_benchmark_pack_runs_status", table_name="benchmark_pack_runs")
    op.drop_index("ix_benchmark_pack_runs_benchmark_pack_id", table_name="benchmark_pack_runs")
    op.drop_table("benchmark_pack_runs")
