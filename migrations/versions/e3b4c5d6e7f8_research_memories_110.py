"""research_memories_110

Revision ID: e3b4c5d6e7f8
Revises: d2a3b4c5d6e7
Create Date: 2026-08-08 13:00:00.000000

issue #110:研究长期记忆 / 研究笔记持久化。
记忆通过 source_refs 关联数据集 / 策略 / 实验 / ResearchRun / Simulation 产物,
不写入实盘 orders/fills/positions/audit_logs。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3b4c5d6e7f8"
down_revision = "d2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_memories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("memory_id", sa.String(length=32), nullable=False),
        sa.Column("memory_type", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=True),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.String(length=32), nullable=True),
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
        "ix_research_memories_memory_id",
        "research_memories",
        ["memory_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_memories_memory_type",
        "research_memories",
        ["memory_type"],
    )
    op.create_index(
        "ix_research_memories_status", "research_memories", ["status"]
    )
    op.create_index(
        "ix_research_memories_conversation_id",
        "research_memories",
        ["conversation_id"],
    )
    op.create_index(
        "ix_research_memories_supersedes_id",
        "research_memories",
        ["supersedes_id"],
    )
    op.create_index(
        "ix_research_memories_created_at", "research_memories", ["created_at"]
    )
    op.create_index(
        "ix_research_memory_status_type_created",
        "research_memories",
        ["status", "memory_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_memory_status_type_created", table_name="research_memories"
    )
    op.drop_index(
        "ix_research_memories_created_at", table_name="research_memories"
    )
    op.drop_index(
        "ix_research_memories_supersedes_id", table_name="research_memories"
    )
    op.drop_index(
        "ix_research_memories_conversation_id", table_name="research_memories"
    )
    op.drop_index("ix_research_memories_status", table_name="research_memories")
    op.drop_index(
        "ix_research_memories_memory_type", table_name="research_memories"
    )
    op.drop_index(
        "ix_research_memories_memory_id", table_name="research_memories"
    )
    op.drop_table("research_memories")
