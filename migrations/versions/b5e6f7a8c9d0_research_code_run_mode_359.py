"""research_code_run_mode_359

Revision ID: b5e6f7a8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-07 10:00:00.000000

issue #359:``research_code_runs`` 新增 ``mode`` 列(factor|factor_series)。

kind=factor 的沙箱执行分双轨:v1 单日截面(``factor.compute``,既有
语义)与 v2 区间执行(``factor.compute_series``,窗口挂载 v3 + 前缀
不变性审计)。列类型为 VARCHAR(16)(文本,无枚举约束 —— 新 mode 值
无需再迁移),``server_default='factor'`` 让历史行与未声明 mode 的入队
payload 自然落到 v1 路径;kind=strategy 的行恒为 'factor'(占位,不参与
strategy 协议语义)。

回滚:drop 列(纯展示/审计维度,无外键依赖)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5e6f7a8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_code_runs",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default="factor",
        ),
    )


def downgrade() -> None:
    op.drop_column("research_code_runs", "mode")
