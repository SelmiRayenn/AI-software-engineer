"""Repository file and symbol indexing.

Revision ID: 20260905_0002
Revises: 20260904_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "20260905_0002"
down_revision = "20260904_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repository_indexes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("skipped_counts", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_repository_indexes_agent_run_id", "repository_indexes", ["agent_run_id"], unique=True
    )
    op.create_table(
        "indexed_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_index_id",
            sa.Uuid(),
            sa.ForeignKey("repository_indexes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("extension", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(100), nullable=False),
        sa.Column("file_type", sa.String(20), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("imports", sa.JSON(), nullable=False),
        sa.Column("python_parse_error", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("repository_index_id", "file_path", name="uq_indexed_files_index_path"),
    )
    op.create_index(
        "ix_indexed_files_repository_index_id", "indexed_files", ["repository_index_id"]
    )
    op.create_table(
        "indexed_symbols",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "indexed_file_id",
            sa.Uuid(),
            sa.ForeignKey("indexed_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("end_line_number", sa.Integer(), nullable=False),
    )
    op.create_index("ix_indexed_symbols_indexed_file_id", "indexed_symbols", ["indexed_file_id"])


def downgrade() -> None:
    op.drop_table("indexed_symbols")
    op.drop_table("indexed_files")
    op.drop_table("repository_indexes")
