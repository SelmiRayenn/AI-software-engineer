"""Add benchmark import provenance and trusted evaluation payload storage."""

import sqlalchemy as sa

from alembic import op

revision = "20260919_0009"
down_revision = "20260916_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.alter_column("issue_number", existing_type=sa.Integer(), nullable=True)
    op.create_table(
        "benchmark_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("task_id", sa.String(255), nullable=False),
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("environment_setup_commit", sa.String(64), nullable=True),
        sa.Column("hints_text", sa.Text(), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("benchmark_task_id"),
    )
    with op.batch_alter_table("hidden_eval_tests") as batch:
        batch.add_column(sa.Column("patch_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("evaluation_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hidden_eval_tests") as batch:
        batch.drop_column("evaluation_metadata")
        batch.drop_column("patch_text")
    op.drop_table("benchmark_imports")
    # Older releases use zero for drafts without a verified GitHub issue.
    op.execute(sa.text("UPDATE benchmark_tasks SET issue_number = 0 WHERE issue_number IS NULL"))
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.alter_column("issue_number", existing_type=sa.Integer(), nullable=False)
