"""mcp_audit_events_157

Revision ID: a9d4e6f8b1c2
Revises: c2d3e4f5a6b7
Create Date: 2026-08-14 10:00:00.000000

issue #157:把 ``mcp_audit_persist`` 配置实际接线 —— MCP 工具调用审计持久化到
独立的 ``mcp_audit_events`` 表(此前只有 structlog + 内存副本,重启即失)。

边界:只追加、不修改、不删除;不写入实盘 ``audit_logs``;入参进入审计前必须经
``summarize_arguments`` 脱敏 / 截断。默认 ``FINBOARD_MCP_AUDIT_PERSIST=false``
不写库。回滚方案:``alembic downgrade -1`` + 关闭 mcp_audit_persist 配置。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9d4e6f8b1c2"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("caller", sa.String(length=128), nullable=True),
        sa.Column("arguments_summary", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_audit_events_operation_id",
        "mcp_audit_events",
        ["operation_id"],
        unique=False,
    )
    op.create_index(
        "ix_mcp_audit_events_tool_name",
        "mcp_audit_events",
        ["tool_name"],
        unique=False,
    )
    op.create_index(
        "ix_mcp_audit_events_recorded_at",
        "mcp_audit_events",
        ["recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_audit_events_recorded_at", table_name="mcp_audit_events")
    op.drop_index("ix_mcp_audit_events_tool_name", table_name="mcp_audit_events")
    op.drop_index("ix_mcp_audit_events_operation_id", table_name="mcp_audit_events")
    op.drop_table("mcp_audit_events")
