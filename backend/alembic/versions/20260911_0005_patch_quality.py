"""Add patch quality reports and task-level dependency allowances."""

import sqlalchemy as sa

from alembic import op

revision = "20260911_0005"
down_revision = "20260909_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.add_column(
            sa.Column(
                "allow_lockfile_changes",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "allow_dependency_file_changes",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "patch_qualities",
        sa.Column("generated_patch_id", sa.Uuid(), nullable=False),
        sa.Column("changed_file_count", sa.Integer(), nullable=False),
        sa.Column("added_lines", sa.Integer(), nullable=False),
        sa.Column("removed_lines", sa.Integer(), nullable=False),
        sa.Column("total_changed_lines", sa.Integer(), nullable=False),
        sa.Column("max_patch_files", sa.Integer(), nullable=False),
        sa.Column("max_patch_changed_lines", sa.Integer(), nullable=False),
        sa.Column("changed_source_files", sa.JSON(), nullable=False),
        sa.Column("changed_test_files", sa.JSON(), nullable=False),
        sa.Column("changed_docs_config_files", sa.JSON(), nullable=False),
        sa.Column("suspicious_generated_files", sa.JSON(), nullable=False),
        sa.Column("unrelated_files", sa.JSON(), nullable=False),
        sa.Column("whitespace_only", sa.Boolean(), nullable=False),
        sa.Column("dependency_files", sa.JSON(), nullable=False),
        sa.Column("lockfiles", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("hard_limit_violations", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["generated_patch_id"], ["generated_patches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_patch_qualities_generated_patch_id",
        "patch_qualities",
        ["generated_patch_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_patch_qualities_generated_patch_id", table_name="patch_qualities")
    op.drop_table("patch_qualities")
    with op.batch_alter_table("benchmark_tasks") as batch:
        batch.drop_column("allow_dependency_file_changes")
        batch.drop_column("allow_lockfile_changes")
