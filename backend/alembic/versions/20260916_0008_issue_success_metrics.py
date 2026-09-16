"""Add issue-specific success metrics."""

import sqlalchemy as sa

from alembic import op

revision = "20260916_0008"
down_revision = "20260911_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.add_column(
            sa.Column(
                "baseline_tests_passed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "post_patch_tests_passed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("issue_resolved", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "regression_detected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "issue_specific_score",
                sa.Float(),
                nullable=False,
                server_default="0",
            )
        )

    op.execute(
        sa.text(
            "UPDATE evaluation_metrics SET baseline_tests_passed = CASE "
            "WHEN EXISTS (SELECT 1 FROM test_results "
            "WHERE test_results.agent_run_id = evaluation_metrics.agent_run_id "
            "AND test_results.phase = 'baseline') "
            "AND NOT EXISTS (SELECT 1 FROM test_results "
            "WHERE test_results.agent_run_id = evaluation_metrics.agent_run_id "
            "AND test_results.phase = 'baseline' AND test_results.passed = false) "
            "THEN true ELSE false END, "
            "post_patch_tests_passed = tests_passed"
        )
    )
    op.execute(
        sa.text(
            "UPDATE evaluation_metrics SET "
            "issue_resolved = CASE WHEN post_patch_tests_passed = true "
            "AND (hidden_tests_run_count = 0 OR hidden_tests_passed = true) "
            "THEN true ELSE false END, "
            "regression_detected = CASE WHEN baseline_tests_passed = true "
            "AND post_patch_tests_passed = false THEN true ELSE false END, "
            "issue_specific_score = CASE "
            "WHEN post_patch_tests_passed = true AND hidden_tests_passed = true THEN 1.0 "
            "WHEN post_patch_tests_passed = true AND hidden_tests_run_count = 0 THEN 0.75 "
            "WHEN post_patch_tests_passed = false AND hidden_tests_passed = true THEN 0.5 "
            "ELSE 0.0 END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("evaluation_metrics") as batch:
        batch.drop_column("issue_specific_score")
        batch.drop_column("regression_detected")
        batch.drop_column("issue_resolved")
        batch.drop_column("post_patch_tests_passed")
        batch.drop_column("baseline_tests_passed")
