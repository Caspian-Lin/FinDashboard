"""backtest_run_research_matching_v2

Revision ID: 8c1f9b3a4d2e
Revises: 560f1271f3f6
Create Date: 2026-07-27 10:00:00.000000

issue #56: 为 ``backtest_runs`` 增加 ``matching_model`` / ``asset_rules`` /
``fee_assumptions`` / ``benchmark_config`` 列,归档研究级成交语义。
旧记录在读取时返回空 dict / None,前端 / API 都按可选字段处理。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8c1f9b3a4d2e"
down_revision = "560f1271f3f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column(
            "matching_model",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("asset_rules", sa.JSON(), nullable=True),
    )
    op.add_column(
        "backtest_runs",
        sa.Column(
            "fee_assumptions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "backtest_runs",
        sa.Column(
            "benchmark_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "benchmark_config")
    op.drop_column("backtest_runs", "fee_assumptions")
    op.drop_column("backtest_runs", "asset_rules")
    op.drop_column("backtest_runs", "matching_model")
