"""watchlists + backtest_runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-26 06:00:00.000000

新增表:
* watchlists / watchlist_items —— 用户标的组(watchlist),保存常用回测标的集合。
* backtest_runs —— 回测运行历史(参数 + 结果),支持历史切换查看。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- watchlists --
    op.create_table(
        "watchlists",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_watchlists_name", "watchlists", ["name"])

    # -- watchlist_items --
    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "watchlist_id",
            sa.BigInteger(),
            sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol_code", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "watchlist_id", "symbol_code", name="uq_watchlist_item"
        ),
    )
    op.create_index(
        "ix_watchlist_items_watchlist_id", "watchlist_items", ["watchlist_id"]
    )
    op.create_index(
        "ix_watchlist_items_symbol_code", "watchlist_items", ["symbol_code"]
    )

    # -- backtest_runs --
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("start", sa.String(16), nullable=False),
        sa.Column("end", sa.String(16), nullable=False),
        sa.Column("capital", sa.Numeric(20, 4), nullable=False),
        sa.Column("adjust", sa.String(8), nullable=False, server_default="qfq"),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("equity_curve", sa.JSON(), nullable=False),
        sa.Column("fills", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_backtest_runs_strategy", "backtest_runs", ["strategy"])
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_created_at", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_strategy", table_name="backtest_runs")
    op.drop_table("backtest_runs")

    op.drop_index("ix_watchlist_items_symbol_code", table_name="watchlist_items")
    op.drop_index("ix_watchlist_items_watchlist_id", table_name="watchlist_items")
    op.drop_table("watchlist_items")

    op.drop_index("ix_watchlists_name", table_name="watchlists")
    op.drop_table("watchlists")
