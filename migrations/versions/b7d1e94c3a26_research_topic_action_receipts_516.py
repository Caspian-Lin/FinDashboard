"""Topic action receipts for lost-receipt idempotency (#516).

Revision ID: b7d1e94c3a26
Revises: 9c4f2a7d1e63
Create Date: 2026-11-10 01:40:00.000000

``research_topic_links`` is unique on ``(topic, entity_type, entity_id)``, so a
second action against the same artifact (for example ``add_trial`` called twice
on one experiment) cannot be anchored on the link's ``operation_id``. Without a
separate per-request anchor a lost response would make the client retry and
execute the action a second time (extra trial, consumed budget, polluted OOS
statistics).

``research_topic_action_receipts`` stores one row per ``(topic_id,
operation_id)`` with the small result projection (entity/job ids + created
flags), written **in the same transaction** as the artifact, the link and the
audit event. A replay returns the stored result and never re-executes.

No foreign keys / no cascade (same convention as the #500/#515 topic tables):
deleting a topic or artifact keeps the receipt for forensics and vice versa.
Research domain only -- never writes live ``orders`` / ``fills`` /
``positions`` / ``audit_logs``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d1e94c3a26"
down_revision = "9c4f2a7d1e63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_topic_action_receipts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("topic_id", sa.String(32), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(48), nullable=True),
        sa.Column("result_code", sa.String(64), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "topic_id",
            "operation_id",
            name="uq_research_topic_action_receipts_topic_operation",
        ),
    )
    op.create_index(
        "ix_research_topic_action_receipts_topic_id",
        "research_topic_action_receipts",
        ["topic_id"],
    )
    op.create_index(
        "ix_research_topic_action_receipts_entity",
        "research_topic_action_receipts",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_research_topic_action_receipts_entity_id",
        "research_topic_action_receipts",
        ["entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_topic_action_receipts_entity_id",
        table_name="research_topic_action_receipts",
    )
    op.drop_index(
        "ix_research_topic_action_receipts_entity",
        table_name="research_topic_action_receipts",
    )
    op.drop_index(
        "ix_research_topic_action_receipts_topic_id",
        table_name="research_topic_action_receipts",
    )
    op.drop_table("research_topic_action_receipts")
