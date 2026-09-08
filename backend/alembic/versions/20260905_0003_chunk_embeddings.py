"""Optional repository chunks and embeddings.

Revision ID: 20260905_0003
Revises: 20260905_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "20260905_0003"
down_revision = "20260905_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "indexed_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "indexed_file_id",
            sa.Uuid(),
            sa.ForeignKey("indexed_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("symbol_names", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.UniqueConstraint("indexed_file_id", "ordinal", name="uq_indexed_chunks_file_ordinal"),
    )
    op.create_index("ix_indexed_chunks_indexed_file_id", "indexed_chunks", ["indexed_file_id"])
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "indexed_chunk_id",
            sa.Uuid(),
            sa.ForeignKey("indexed_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("input_checksum", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_chunk_embeddings_indexed_chunk_id",
        "chunk_embeddings",
        ["indexed_chunk_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("chunk_embeddings")
    op.drop_table("indexed_chunks")
