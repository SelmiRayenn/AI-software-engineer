"""Add queryable agent run failure classifications."""

import sqlalchemy as sa

from alembic import op

revision = "20260911_0006"
down_revision = "20260911_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_run_failures",
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("human_readable_summary", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_event_id"], ["agent_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_run_failures_agent_run_id",
        "agent_run_failures",
        ["agent_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_run_failures_category",
        "agent_run_failures",
        ["category"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_failures_category", table_name="agent_run_failures")
    op.drop_index("ix_agent_run_failures_agent_run_id", table_name="agent_run_failures")
    op.drop_table("agent_run_failures")
