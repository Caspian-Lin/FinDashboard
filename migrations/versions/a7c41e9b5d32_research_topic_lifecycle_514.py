"""Research topic lifecycle columns and indexes (#514).

Adds the queryable lifecycle state, activity timestamp and create idempotency
key to ``research_topics``. The JSON payload keeps ``status`` for backward
compatibility, but the column is authoritative on read.

Revision ID: a7c41e9b5d32
Revises: d3e4f5a6b7c8
"""

from alembic import op

revision = "a7c41e9b5d32"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "research_topics",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column(
        "research_topics",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "research_topics",
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        "ck_research_topics_status",
        "research_topics",
        "status IN ('draft', 'active', 'paused', 'closed')",
    )
    # Legacy rows only carried the status inside the JSON payload; unknown or
    # missing values fall back to the previous implicit default (active).
    op.execute(
        "UPDATE research_topics SET status = CASE "
        "WHEN payload::jsonb->>'status' IN ('draft', 'active', 'paused', 'closed') "
        "THEN payload::jsonb->>'status' ELSE 'active' END"
    )
    op.create_index(
        "uq_research_topics_idempotency_key",
        "research_topics",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_research_topics_status_updated",
        "research_topics",
        ["status", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    # Keep the payload lossless: statuses written by the lifecycle endpoints are
    # already double-written, but sync once more before dropping the column.
    op.execute(
        "UPDATE research_topics SET payload = jsonb_set("
        "payload::jsonb, '{status}', to_jsonb(status), true)::json"
    )
    op.drop_index("ix_research_topics_status_updated", table_name="research_topics")
    op.drop_index("uq_research_topics_idempotency_key", table_name="research_topics")
    op.drop_constraint("ck_research_topics_status", "research_topics", type_="check")
    op.drop_column("research_topics", "idempotency_key")
    op.drop_column("research_topics", "updated_at")
    op.drop_column("research_topics", "status")
