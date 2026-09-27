"""Persist explainable patch minimization analysis."""

import sqlalchemy as sa

from alembic import op

revision = "20260926_0014"
down_revision = "20260920_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("patch_qualities") as batch:
        batch.add_column(
            sa.Column("minimization_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("changed_hunk_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("added_removed_ratio", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("file_kind_counts", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.add_column(
            sa.Column("duplicate_edit_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "formatting_only_hunk_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "unrelated_formatting_hunk_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column("large_rewrite_hunk_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("generated_block_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "uninspected_files", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
            )
        )
        batch.add_column(sa.Column("minimization_score", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column(
                "minimization_warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
            )
        )
        batch.add_column(
            sa.Column(
                "minimization_penalties", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("patch_qualities") as batch:
        batch.drop_column("minimization_penalties")
        batch.drop_column("minimization_warnings")
        batch.drop_column("minimization_score")
        batch.drop_column("uninspected_files")
        batch.drop_column("generated_block_count")
        batch.drop_column("large_rewrite_hunk_count")
        batch.drop_column("unrelated_formatting_hunk_count")
        batch.drop_column("formatting_only_hunk_count")
        batch.drop_column("duplicate_edit_count")
        batch.drop_column("file_kind_counts")
        batch.drop_column("added_removed_ratio")
        batch.drop_column("changed_hunk_count")
        batch.drop_column("minimization_version")
