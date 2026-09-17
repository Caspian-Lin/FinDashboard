"""opencode_agent_conversations_109

Revision ID: d2a3b4c5d6e7
Revises: c101a2b3d4e5
Create Date: 2026-08-08 12:00:00.000000

issue #109:OpenCode 研究运行时接入 —— 会话关联与事件持久化。
所有表只服务研究运行时, 不写入实盘 orders/fills/positions/audit_logs。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2a3b4c5d6e7"
down_revision = "c101a2b3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- agent_conversations ---
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("opencode_session_id", sa.String(length=64), nullable=False),
        sa.Column("agent_run_id", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("model_ref", sa.String(length=128), nullable=True),
        sa.Column("last_event_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_conversations_conversation_id",
        "agent_conversations",
        ["conversation_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_conversations_opencode_session_id",
        "agent_conversations",
        ["opencode_session_id"],
    )
    op.create_index(
        "ix_agent_conversations_agent_run_id",
        "agent_conversations",
        ["agent_run_id"],
    )
    op.create_index(
        "ix_agent_conversations_status", "agent_conversations", ["status"]
    )
    op.create_index(
        "ix_agent_conversations_created_at", "agent_conversations", ["created_at"]
    )
    op.create_index(
        "ix_agent_conv_status_created",
        "agent_conversations",
        ["status", "created_at"],
    )

    # --- agent_events ---
    op.create_table(
        "agent_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "event_seq", name="uq_agent_event_conv_seq"
        ),
    )
    op.create_index(
        "ix_agent_events_conversation_id", "agent_events", ["conversation_id"]
    )
    op.create_index("ix_agent_events_event_type", "agent_events", ["event_type"])
    op.create_index("ix_agent_events_timestamp", "agent_events", ["timestamp"])
    op.create_index(
        "ix_agent_event_conv_type_seq",
        "agent_events",
        ["conversation_id", "event_type", "event_seq"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_event_conv_type_seq", table_name="agent_events"
    )
    op.drop_index("ix_agent_events_timestamp", table_name="agent_events")
    op.drop_index("ix_agent_events_event_type", table_name="agent_events")
    op.drop_index("ix_agent_events_conversation_id", table_name="agent_events")
    op.drop_table("agent_events")

    op.drop_index("ix_agent_conv_status_created", table_name="agent_conversations")
    op.drop_index(
        "ix_agent_conversations_created_at", table_name="agent_conversations"
    )
    op.drop_index("ix_agent_conversations_status", table_name="agent_conversations")
    op.drop_index(
        "ix_agent_conversations_agent_run_id", table_name="agent_conversations"
    )
    op.drop_index(
        "ix_agent_conversations_opencode_session_id", table_name="agent_conversations"
    )
    op.drop_index(
        "ix_agent_conversations_conversation_id", table_name="agent_conversations"
    )
    op.drop_table("agent_conversations")
