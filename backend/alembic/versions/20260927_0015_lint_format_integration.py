"""Add configured lint and format-check evaluation fields."""

import sqlalchemy as sa

from alembic import op

revision = "20260927_0015"
down_revision = "20260926_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.add_column(
            sa.Column("lint_commands", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column(
                "format_check_commands",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.add_column(sa.Column("lint_passed", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("format_check_passed", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("code_quality_passed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.drop_column("code_quality_passed")
        batch.drop_column("format_check_passed")
        batch.drop_column("lint_passed")
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.drop_column("format_check_commands")
        batch.drop_column("lint_commands")
