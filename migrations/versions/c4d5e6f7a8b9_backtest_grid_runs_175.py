"""backtest_grid_runs_175

Revision ID: c4d5e6f7a8b9
Revises: c3d9a1b2e4f5
Create Date: 2026-08-16 14:00:00.000000

issue #175:backtest_grid_runs 表 —— 批量参数网格回测定义。一次提交 N 组参数,
网格行记录组合展开结果 + 逐组合的 backtest_run 任务 job_id(字符串引用,
不建外键,与 background_jobs 同风格)。聚合对比表由 MCP ``grid_get`` 按
grid_id 实时计算,不在本表缓存结果。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "c3d9a1b2e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_grid_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "grid_id", sa.String(length=48), nullable=False, unique=True, index=True
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("combos_checksum", sa.String(length=64), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False, index=True),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("start", sa.String(length=16), nullable=False),
        sa.Column("end", sa.String(length=16), nullable=False),
        sa.Column(
            "capital",
            sa.Numeric(20, 4, decimal_return_scale=4),
            nullable=False,
        ),
        sa.Column("adjust", sa.String(length=8), nullable=False),
        sa.Column("base_params", sa.JSON(), nullable=False),
        sa.Column(
            "selection",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "combos",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "combo_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "max_combos", sa.Integer(), server_default="20", nullable=False
        ),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_backtest_grid_runs_key"),
    )
    op.create_index(
        "ix_backtest_grid_runs_idempotency_key",
        "backtest_grid_runs",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_grid_runs_idempotency_key", table_name="backtest_grid_runs"
    )
    op.drop_table("backtest_grid_runs")
