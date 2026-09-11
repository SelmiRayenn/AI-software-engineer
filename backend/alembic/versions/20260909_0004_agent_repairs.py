"""Preserve patch versions and associate test results with repair attempts."""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0004"
down_revision = "20260905_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(
            sa.Column("repair_attempts_used", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("final_patch_passed_tests", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("failure_summary", sa.Text(), nullable=True))
    with op.batch_alter_table("generated_patches") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.drop_index("ix_generated_patches_agent_run_id")
        batch.create_index("ix_generated_patches_agent_run_id", ["agent_run_id"], unique=False)
        batch.create_unique_constraint("uq_patch_run_version", ["agent_run_id", "version"])
    with op.batch_alter_table("test_results") as batch:
        batch.add_column(sa.Column("generated_patch_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("attempt_number", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_test_result_patch",
            "generated_patches",
            ["generated_patch_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Existing runs had exactly one candidate. Keep reviews, IDs and historical test evidence.
    op.execute(sa.text("UPDATE generated_patches SET is_selected = true"))
    op.execute(
        sa.text(
            "UPDATE test_results SET generated_patch_id = "
            "(SELECT id FROM generated_patches WHERE agent_run_id = test_results.agent_run_id) "
            "WHERE phase = 'post_patch'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE agent_runs SET final_patch_passed_tests = "
            "CASE WHEN EXISTS (SELECT 1 FROM test_results WHERE agent_run_id = agent_runs.id "
            "AND phase = 'post_patch' AND passed = false) THEN false ELSE true END "
            "WHERE EXISTS (SELECT 1 FROM test_results WHERE agent_run_id = agent_runs.id "
            "AND phase = 'post_patch' AND generated_patch_id IS NOT NULL)"
        )
    )


def downgrade() -> None:
    # The old schema cannot retain multiple candidates; never silently discard review history.
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT agent_run_id FROM generated_patches GROUP BY agent_run_id HAVING COUNT(*) > 1"
            )
        )
        .first()
    ):
        raise RuntimeError(
            "Cannot downgrade while runs have multiple patch versions. Archive them first."
        )
    with op.batch_alter_table("test_results") as batch:
        batch.drop_constraint("fk_test_result_patch", type_="foreignkey")
        batch.drop_column("attempt_number")
        batch.drop_column("generated_patch_id")
    with op.batch_alter_table("generated_patches") as batch:
        batch.drop_constraint("uq_patch_run_version", type_="unique")
        batch.drop_index("ix_generated_patches_agent_run_id")
        batch.create_index("ix_generated_patches_agent_run_id", ["agent_run_id"], unique=True)
        batch.drop_column("is_selected")
        batch.drop_column("version")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("failure_summary")
        batch.drop_column("final_patch_passed_tests")
        batch.drop_column("repair_attempts_used")
