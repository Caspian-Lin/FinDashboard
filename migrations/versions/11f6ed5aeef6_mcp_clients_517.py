"""MCP per-client credentials (#517).

Revision ID: 11f6ed5aeef6
Revises: d3e4f5a6b7c8
Create Date: 2026-11-09 16:05:00.000000

issue #517:独立研究 MCP 的按客户端授权。

* ``mcp_clients`` —— 服务端签发的可撤销凭据。只存 ``sha256(token)``,不存明文;
  ``token_prefix`` 仅用于人工辨认,不能用于认证;
* ``scopes`` 是 JSON 数组(``research.read`` / ``research.write`` /
  ``research.run`` / ``data.write`` / ``audit.read``);权限在 ``run_tool``
  唯一入口按工具作用域表强制,缺表项 fail-closed;
* ``revoked_at`` / ``expires_at`` 非空即拒绝;``last_used_at`` 供运营审计,
  已吊销的凭据不会再更新它。

``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 不落库(启动时合成 ``MC-legacy``,
下一版本移除)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "11f6ed5aeef6"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_clients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("client_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mcp_clients_client_id", "mcp_clients", ["client_id"], unique=True)
    op.create_index("ix_mcp_clients_name", "mcp_clients", ["name"], unique=True)
    op.create_index("ix_mcp_clients_token_hash", "mcp_clients", ["token_hash"])


def downgrade() -> None:
    op.drop_index("ix_mcp_clients_token_hash", table_name="mcp_clients")
    op.drop_index("ix_mcp_clients_name", table_name="mcp_clients")
    op.drop_index("ix_mcp_clients_client_id", table_name="mcp_clients")
    op.drop_table("mcp_clients")
