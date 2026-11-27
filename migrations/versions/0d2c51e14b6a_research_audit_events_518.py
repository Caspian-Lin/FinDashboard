"""Research operation audit trail (#518).

Revision ID: 0d2c51e14b6a
Revises: 11f6ed5aeef6
Create Date: 2026-11-09 20:45:00.000000

issue #518:串联 Web / MCP / worker 的研究操作审计。

``research_audit_events`` 是研究域的**只追加**操作账本:

* ``trust='server_observed'`` —— 服务端在同一事务内观察到的事实
  (成功写与业务写入同事务;拒绝/失败路径用独立会话补记);
* ``trust='client_declared'`` —— 外部 Harness 经
  ``POST /api/research/audit/client-events`` 上报的调用/工具事件,
  与 ``server_observed`` 并列展示但**永不覆盖**服务端事实。

无外键、无级联:删除历史只需删表;不触碰实盘 ``audit_logs``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0d2c51e14b6a"
down_revision = "11f6ed5aeef6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("audit_id", sa.String(32), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("client_id", sa.String(32), nullable=True),
        sa.Column("topic_id", sa.String(32), nullable=True),
        sa.Column("entry_id", sa.String(32), nullable=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("result_code", sa.String(64), nullable=True),
        sa.Column("correlation", sa.JSON(), nullable=False),
        sa.Column("params_summary", sa.JSON(), nullable=False),
        sa.Column("params_digest", sa.String(64), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column(
            "trust",
            sa.String(16),
            server_default="server_observed",
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'rejected', 'denied')",
            name="ck_research_audit_events_outcome",
        ),
        sa.CheckConstraint(
            "trust IN ('server_observed', 'client_declared')",
            name="ck_research_audit_events_trust",
        ),
    )
    op.create_index(
        "ix_research_audit_events_audit_id",
        "research_audit_events",
        ["audit_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_audit_events_occurred",
        "research_audit_events",
        [sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_research_audit_events_topic_occurred",
        "research_audit_events",
        ["topic_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_research_audit_events_client_occurred",
        "research_audit_events",
        ["client_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_research_audit_events_entity",
        "research_audit_events",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_research_audit_events_outcome",
        "research_audit_events",
        ["outcome"],
    )
    # 客户端声明事件按 (client_id, client_event_id) 去重;client_event_id 复用
    # ``idempotency_key`` 列(见 docs/research/audit-operations.md 数据字典)。
    op.create_index(
        "uq_research_audit_client_event",
        "research_audit_events",
        ["client_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "trust = 'client_declared' AND idempotency_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_research_audit_client_event", table_name="research_audit_events")
    op.drop_index("ix_research_audit_events_outcome", table_name="research_audit_events")
    op.drop_index("ix_research_audit_events_entity", table_name="research_audit_events")
    op.drop_index(
        "ix_research_audit_events_client_occurred", table_name="research_audit_events"
    )
    op.drop_index(
        "ix_research_audit_events_topic_occurred", table_name="research_audit_events"
    )
    op.drop_index(
        "ix_research_audit_events_occurred", table_name="research_audit_events"
    )
    op.drop_index(
        "ix_research_audit_events_audit_id", table_name="research_audit_events"
    )
    op.drop_table("research_audit_events")
