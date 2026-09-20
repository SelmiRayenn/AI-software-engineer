"""Add versioned benchmark packs."""

import sqlalchemy as sa

from alembic import op

revision = "20260920_0010"
down_revision = "20260919_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("source", sa.String(2048), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_benchmark_packs_slug", "benchmark_packs", ["slug"], unique=True)
    op.create_table(
        "benchmark_pack_tasks",
        sa.Column("benchmark_pack_id", sa.Uuid(), nullable=False),
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("difficulty", sa.String(100), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["benchmark_pack_id"], ["benchmark_packs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("benchmark_pack_id", "benchmark_task_id"),
        sa.UniqueConstraint(
            "benchmark_pack_id", "order_index", name="uq_benchmark_pack_tasks_pack_order"
        ),
    )


def downgrade() -> None:
    op.drop_table("benchmark_pack_tasks")
    op.drop_index("ix_benchmark_packs_slug", table_name="benchmark_packs")
    op.drop_table("benchmark_packs")
