"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-21 11:28:33.000000

P0 初始 schema:accounts / orders / fills / positions / reconciliation_logs / audit_logs。
所有金额、数量字段统一 ``NUMERIC(20, 4)``;时间戳统一 ``TIMESTAMPTZ``。

后续迁移请用 ``make migrate-new name=<slug>`` 生成,不要手写 revision id。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(64), nullable=False, unique=True),
        sa.Column("broker_kind", sa.String(16), nullable=False),
        sa.Column("total_asset", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("cash", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("frozen_cash", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("margin_used", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_accounts_account_id", "accounts", ["account_id"], unique=True)

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("broker_kind", sa.String(16), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False),
        sa.Column("time_in_force", sa.String(8), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("average_fill_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reject_reason", sa.String(32), nullable=True),
        sa.Column("reject_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("risk_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_orders_client_order_id", "orders", ["client_order_id"], unique=True)
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_account_id", "orders", ["account_id"])
    op.create_index("ix_orders_strategy_id", "orders", ["strategy_id"])
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])
    op.create_index("ix_orders_account_status", "orders", ["account_id", "status"])
    op.create_index("ix_orders_symbol_created", "orders", ["symbol", "created_at"])

    op.create_table(
        "fills",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fill_id", sa.String(64), nullable=False, unique=True),
        sa.Column("broker_fill_id", sa.String(64), nullable=True),
        sa.Column(
            "client_order_id",
            sa.String(64),
            sa.ForeignKey("orders.client_order_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 4), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column("commission", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("tax", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column(
            "filled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_fills_fill_id", "fills", ["fill_id"], unique=True)
    op.create_index("ix_fills_broker_fill_id", "fills", ["broker_fill_id"])
    op.create_index("ix_fills_client_order_id", "fills", ["client_order_id"])
    op.create_index("ix_fills_symbol", "fills", ["symbol"])
    op.create_index("ix_fills_filled_at", "fills", ["filled_at"])

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("total_quantity", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("available_quantity", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("frozen_quantity", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("average_price", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("market_value", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "account_id",
            "symbol",
            "position_side",
            "source",
            name="uq_positions_account_symbol_side_source",
        ),
    )
    op.create_index("ix_positions_account_id", "positions", ["account_id"])
    op.create_index("ix_positions_symbol", "positions", ["symbol"])

    op.create_table(
        "reconciliation_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("local_state", sa.Text(), nullable=True),
        sa.Column("broker_state", sa.Text(), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("action_taken", sa.String(32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_reconciliation_logs_account_id", "reconciliation_logs", ["account_id"])
    op.create_index("ix_reconciliation_logs_snapshot_at", "reconciliation_logs", ["snapshot_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target", sa.String(128), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("reconciliation_logs")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("accounts")
