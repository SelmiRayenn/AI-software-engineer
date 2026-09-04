"""initial schema

Revision ID: 20260904_0001
Revises:
Create Date: 2026-09-04 00:01:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner", "name", name="uq_repositories_owner_name"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_repositories_name", "repositories", ["name"], unique=False)
    op.create_index("ix_repositories_owner", "repositories", ["owner"], unique=False)

    op.create_table(
        "benchmark_tasks",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_title", sa.String(length=500), nullable=False),
        sa.Column("issue_body", sa.Text(), nullable=True),
        sa.Column("issue_comments", sa.JSON(), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
        sa.Column("base_commit", sa.String(length=64), nullable=False),
        sa.Column("fix_commit", sa.String(length=64), nullable=True),
        sa.Column("linked_pr_url", sa.String(length=2048), nullable=True),
        sa.Column("setup_commands", sa.JSON(), nullable=False),
        sa.Column("test_commands", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_benchmark_tasks_repository_id",
        "benchmark_tasks",
        ["repository_id"],
        unique=False,
    )
    op.create_index("ix_benchmark_tasks_status", "benchmark_tasks", ["status"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("model_provider", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("workspace_id", sa.String(length=100), nullable=True),
        sa.Column("workspace_path", sa.String(length=2048), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_runs_benchmark_task_id",
        "agent_runs",
        ["benchmark_task_id"],
        unique=False,
    )
    op.create_index("ix_agent_runs_model_provider", "agent_runs", ["model_provider"], unique=False)
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"], unique=False)

    op.create_table(
        "gold_patches",
        sa.Column("benchmark_task_id", sa.Uuid(), nullable=False),
        sa.Column("changed_files", sa.JSON(), nullable=False),
        sa.Column("patch_text", sa.Text(), nullable=False),
        sa.Column("test_files", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["benchmark_task_id"], ["benchmark_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gold_patches_benchmark_task_id",
        "gold_patches",
        ["benchmark_task_id"],
        unique=True,
    )

    op.create_table(
        "agent_events",
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_events_agent_run_id", "agent_events", ["agent_run_id"], unique=False)
    op.create_index("ix_agent_events_event_type", "agent_events", ["event_type"], unique=False)

    op.create_table(
        "evaluation_metrics",
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_localization_score", sa.Float(), nullable=True),
        sa.Column("patch_applied", sa.Boolean(), nullable=False),
        sa.Column("tests_passed", sa.Boolean(), nullable=False),
        sa.Column("modified_files_count", sa.Integer(), nullable=False),
        sa.Column("unrelated_files_count", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("execution_time_seconds", sa.Float(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evaluation_metrics_agent_run_id",
        "evaluation_metrics",
        ["agent_run_id"],
        unique=True,
    )

    op.create_table(
        "generated_patches",
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("patch_text", sa.Text(), nullable=False),
        sa.Column("changed_files", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generated_patches_agent_run_id",
        "generated_patches",
        ["agent_run_id"],
        unique=True,
    )

    op.create_table(
        "test_results",
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(length=100), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_results_agent_run_id", "test_results", ["agent_run_id"], unique=False)

    op.create_table(
        "human_reviews",
        sa.Column("generated_patch_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=50), nullable=False),
        sa.Column("reviewer_name", sa.String(length=255), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generated_patch_id"],
            ["generated_patches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_human_reviews_decision", "human_reviews", ["decision"], unique=False)
    op.create_index(
        "ix_human_reviews_generated_patch_id",
        "human_reviews",
        ["generated_patch_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("human_reviews")
    op.drop_table("test_results")
    op.drop_table("generated_patches")
    op.drop_table("evaluation_metrics")
    op.drop_table("agent_events")
    op.drop_table("gold_patches")
    op.drop_table("agent_runs")
    op.drop_table("benchmark_tasks")
    op.drop_table("repositories")
