"""Research topics and append-only rounds (#500).

Revision ID: 62f8b1e7a905
Revises: b8c9d0e1f2a3
"""

from alembic import op

revision = "62f8b1e7a905"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import sqlalchemy as sa

    op.create_table(
        "research_topics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("topic_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_research_topics_topic_id", "research_topics", ["topic_id"], unique=True)
    op.create_table(
        "research_topic_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("entry_id", sa.String(32), nullable=False),
        sa.Column("topic_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("topic_id", "idempotency_key", name="uq_research_topic_entry_key"),
    )
    op.create_index(
        "ix_research_topic_entries_entry_id", "research_topic_entries", ["entry_id"], unique=True
    )
    op.create_index("ix_research_topic_entries_topic_id", "research_topic_entries", ["topic_id"])


def downgrade() -> None:
    op.drop_table("research_topic_entries")
    op.drop_table("research_topics")
