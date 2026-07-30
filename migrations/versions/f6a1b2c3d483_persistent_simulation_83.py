"""persistent_simulation_83

Revision ID: f6a1b2c3d483
Revises: d41e7b9c2a80
Create Date: 2026-07-30 10:00:00.000000

issue #83:独立持久化模拟账户、会话、订单、成交、持仓、账本、行情和审计。
所有实体均使用 simulation 命名空间, 不引用实盘账户/订单/成交/持仓表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a1b2c3d483"
down_revision = "d41e7b9c2a80"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(28, 8)


def upgrade() -> None:
    op.create_table(
        "simulation_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("initial_cash", _MONEY, nullable=False),
        sa.Column("cash", _MONEY, nullable=False),
        sa.Column("frozen_cash", _MONEY, nullable=False),
        sa.Column("margin_used", _MONEY, nullable=False),
        sa.Column("equity", _MONEY, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_simulation_accounts_simulation_account_id",
        "simulation_accounts",
        ["simulation_account_id"],
        unique=True,
    )
    op.create_index("ix_simulation_accounts_status", "simulation_accounts", ["status"])
    op.create_index(
        "ix_simulation_accounts_created_at",
        "simulation_accounts",
        ["created_at"],
    )

    op.create_table(
        "simulation_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("strategy_checksum", sa.String(length=64), nullable=False),
        sa.Column("validation_run_id", sa.String(length=96), nullable=False),
        sa.Column("data_release_id", sa.String(length=128), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column(
            "clock",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("promotion_status", sa.String(length=24), nullable=False),
        sa.Column("reset_of_session_id", sa.String(length=96), nullable=True),
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_account_id"],
            ["simulation_accounts.simulation_account_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id"],
            ["research_runs.run_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_simulation_sessions_simulation_session_id",
        "simulation_sessions",
        ["simulation_session_id"],
        unique=True,
    )
    for name, columns in (
        (
            "ix_simulation_sessions_simulation_account_id",
            ["simulation_account_id"],
        ),
        ("ix_simulation_sessions_status", ["status"]),
        ("ix_simulation_sessions_strategy_id", ["strategy_id"]),
        ("ix_simulation_sessions_validation_run_id", ["validation_run_id"]),
        ("ix_simulation_sessions_data_release_id", ["data_release_id"]),
        ("ix_simulation_sessions_promotion_status", ["promotion_status"]),
        ("ix_simulation_sessions_reset_of_session_id", ["reset_of_session_id"]),
        ("ix_simulation_sessions_created_at", ["created_at"]),
        (
            "ix_simulation_sessions_account_status",
            ["simulation_account_id", "status"],
        ),
    ):
        op.create_index(name, "simulation_sessions", columns)

    op.create_table(
        "simulation_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("source_run_id", sa.String(length=96), nullable=False),
        sa.Column("source_decision_id", sa.String(length=128), nullable=False),
        sa.Column("source_signal_trace_ids", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_session_id"],
            ["simulation_sessions.simulation_session_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "simulation_session_id",
            "decision_id",
            name="uq_simulation_decision",
        ),
    )
    for name, columns in (
        ("ix_simulation_decisions_simulation_session_id", ["simulation_session_id"]),
        ("ix_simulation_decisions_source_run_id", ["source_run_id"]),
        ("ix_simulation_decisions_source_decision_id", ["source_decision_id"]),
        ("ix_simulation_decisions_status", ["status"]),
        ("ix_simulation_decisions_created_at", ["created_at"]),
    ):
        op.create_index(name, "simulation_decisions", columns)

    op.create_table(
        "simulation_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_order_id", sa.String(length=96), nullable=False),
        sa.Column("intent_key", sa.String(length=160), nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("signal_trace_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("instrument_type", sa.String(length=24), nullable=False),
        sa.Column("asset_rule_key", sa.String(length=32), nullable=False),
        sa.Column("position_side", sa.String(length=8), nullable=False),
        sa.Column("position_effect", sa.String(length=8), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("time_in_force", sa.String(length=8), nullable=False),
        sa.Column("quantity", _MONEY, nullable=False),
        sa.Column("price", _MONEY, nullable=True),
        sa.Column("filled_quantity", _MONEY, nullable=False),
        sa.Column("average_fill_price", _MONEY, nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reject_reason", sa.String(length=64), nullable=True),
        sa.Column("reject_message", sa.Text(), nullable=True),
        sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("reserved_cash", _MONEY, nullable=False),
        sa.Column("reserved_quantity", _MONEY, nullable=False),
        sa.Column("submitted_market_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligible_after", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["simulation_session_id"],
            ["simulation_sessions.simulation_session_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_key"),
    )
    op.create_index(
        "ix_simulation_orders_simulation_order_id",
        "simulation_orders",
        ["simulation_order_id"],
        unique=True,
    )
    for name, columns in (
        ("ix_simulation_orders_simulation_account_id", ["simulation_account_id"]),
        ("ix_simulation_orders_simulation_session_id", ["simulation_session_id"]),
        ("ix_simulation_orders_decision_id", ["decision_id"]),
        ("ix_simulation_orders_strategy_id", ["strategy_id"]),
        ("ix_simulation_orders_signal_trace_id", ["signal_trace_id"]),
        ("ix_simulation_orders_symbol", ["symbol"]),
        ("ix_simulation_orders_status", ["status"]),
        ("ix_simulation_orders_eligible_after", ["eligible_after"]),
        ("ix_simulation_orders_created_at", ["created_at"]),
        (
            "ix_simulation_orders_session_status",
            ["simulation_session_id", "status"],
        ),
    ):
        op.create_index(name, "simulation_orders", columns)

    op.create_table(
        "simulation_fills",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_fill_id", sa.String(length=96), nullable=False),
        sa.Column("fill_event_key", sa.String(length=192), nullable=False),
        sa.Column("simulation_order_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("position_side", sa.String(length=8), nullable=False),
        sa.Column("position_effect", sa.String(length=8), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", _MONEY, nullable=False),
        sa.Column("price", _MONEY, nullable=False),
        sa.Column("commission", _MONEY, nullable=False),
        sa.Column("tax", _MONEY, nullable=False),
        sa.Column("slippage_cost", _MONEY, nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_order_id"],
            ["simulation_orders.simulation_order_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fill_event_key"),
    )
    op.create_index(
        "ix_simulation_fills_simulation_fill_id",
        "simulation_fills",
        ["simulation_fill_id"],
        unique=True,
    )
    for name, columns in (
        ("ix_simulation_fills_simulation_order_id", ["simulation_order_id"]),
        ("ix_simulation_fills_simulation_account_id", ["simulation_account_id"]),
        ("ix_simulation_fills_simulation_session_id", ["simulation_session_id"]),
        ("ix_simulation_fills_symbol", ["symbol"]),
        ("ix_simulation_fills_filled_at", ["filled_at"]),
    ):
        op.create_index(name, "simulation_fills", columns)

    op.create_table(
        "simulation_positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("instrument_type", sa.String(length=24), nullable=False),
        sa.Column("asset_rule_key", sa.String(length=32), nullable=False),
        sa.Column("position_side", sa.String(length=8), nullable=False),
        sa.Column("total_quantity", _MONEY, nullable=False),
        sa.Column("available_quantity", _MONEY, nullable=False),
        sa.Column("frozen_quantity", _MONEY, nullable=False),
        sa.Column("average_price", _MONEY, nullable=False),
        sa.Column("market_value", _MONEY, nullable=False),
        sa.Column("realized_pnl", _MONEY, nullable=False),
        sa.Column("unrealized_pnl", _MONEY, nullable=False),
        sa.Column("margin_used", _MONEY, nullable=False),
        sa.Column("last_price", _MONEY, nullable=False),
        sa.Column("last_settlement_price", _MONEY, nullable=False),
        sa.Column("last_settlement_date", sa.Date(), nullable=True),
        sa.Column(
            "lots",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "simulation_account_id",
            "symbol",
            "position_side",
            name="uq_simulation_position",
        ),
    )
    op.create_index(
        "ix_simulation_positions_simulation_account_id",
        "simulation_positions",
        ["simulation_account_id"],
    )
    op.create_index("ix_simulation_positions_symbol", "simulation_positions", ["symbol"])

    op.create_table(
        "simulation_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.String(length=128), nullable=True),
        sa.Column("cash_delta", _MONEY, nullable=False),
        sa.Column("margin_delta", _MONEY, nullable=False),
        sa.Column("realized_pnl", _MONEY, nullable=False),
        sa.Column("commission", _MONEY, nullable=False),
        sa.Column("tax", _MONEY, nullable=False),
        sa.Column("cash_after", _MONEY, nullable=False),
        sa.Column("frozen_cash_after", _MONEY, nullable=False),
        sa.Column("margin_used_after", _MONEY, nullable=False),
        sa.Column("equity_after", _MONEY, nullable=False),
        sa.Column(
            "payload",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "simulation_session_id",
            "sequence",
            name="uq_simulation_ledger_sequence",
        ),
    )
    op.create_index(
        "ix_simulation_ledger_ledger_id",
        "simulation_ledger",
        ["ledger_id"],
        unique=True,
    )
    for name, columns in (
        ("ix_simulation_ledger_simulation_account_id", ["simulation_account_id"]),
        ("ix_simulation_ledger_simulation_session_id", ["simulation_session_id"]),
        ("ix_simulation_ledger_event_type", ["event_type"]),
        ("ix_simulation_ledger_reference_id", ["reference_id"]),
        ("ix_simulation_ledger_occurred_at", ["occurred_at"]),
    ):
        op.create_index(name, "simulation_ledger", columns)

    op.create_table(
        "simulation_market_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=False),
        sa.Column("source_event_id", sa.String(length=160), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_session_id"],
            ["simulation_sessions.simulation_session_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "simulation_session_id",
            "source_event_id",
            name="uq_simulation_market_event",
        ),
    )
    for name, columns in (
        (
            "ix_simulation_market_events_simulation_session_id",
            ["simulation_session_id"],
        ),
        ("ix_simulation_market_events_status", ["status"]),
        ("ix_simulation_market_events_symbol", ["symbol"]),
        ("ix_simulation_market_events_timestamp", ["timestamp"]),
        (
            "ix_simulation_market_symbol_time",
            ["simulation_session_id", "symbol", "timestamp"],
        ),
    ):
        op.create_index(name, "simulation_market_events", columns)

    op.create_table(
        "simulation_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("audit_id", sa.String(length=96), nullable=False),
        sa.Column("event_key", sa.String(length=192), nullable=False),
        sa.Column("simulation_account_id", sa.String(length=96), nullable=False),
        sa.Column("simulation_session_id", sa.String(length=96), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=True),
        sa.Column(
            "payload",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index(
        "ix_simulation_audit_audit_id",
        "simulation_audit",
        ["audit_id"],
        unique=True,
    )
    for name, columns in (
        ("ix_simulation_audit_simulation_account_id", ["simulation_account_id"]),
        ("ix_simulation_audit_simulation_session_id", ["simulation_session_id"]),
        ("ix_simulation_audit_action", ["action"]),
        ("ix_simulation_audit_created_at", ["created_at"]),
    ):
        op.create_index(name, "simulation_audit", columns)


def downgrade() -> None:
    op.drop_table("simulation_audit")
    op.drop_table("simulation_market_events")
    op.drop_table("simulation_ledger")
    op.drop_table("simulation_positions")
    op.drop_table("simulation_fills")
    op.drop_table("simulation_orders")
    op.drop_table("simulation_decisions")
    op.drop_table("simulation_sessions")
    op.drop_table("simulation_accounts")
