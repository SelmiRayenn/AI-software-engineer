"""Add persisted review readiness evaluation fields."""

import sqlalchemy as sa

from alembic import op

revision = "20260928_0016"
down_revision = "20260927_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.add_column(
            sa.Column("code_quality_score", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("review_ready", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("review_blockers", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("review_warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.drop_column("review_warnings")
        batch.drop_column("review_blockers")
        batch.drop_column("review_ready")
        batch.drop_column("code_quality_score")
