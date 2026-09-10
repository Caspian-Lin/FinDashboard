"""predefined_factor_series_kind_398

Revision ID: e9a0b1c2d3f4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-09 12:00:00.000000

issue #398:``research_factor_series.kind`` 列扩宽(String(16) → String(32))。

平台预置因子经 factor_series 通道落库时 ``kind = 'predefined_factor'``
(与 job payload 的 kind 契约一致,17 字符),超出原 VARCHAR(16)
(research_code_runs 同口径 kind='factor' 的历史宽度)。用户因子路径
零变化;纯列宽扩展,无数据改写。

回滚 = downgrade 收回列宽(须先确认库中不存在 >16 字符的 kind 值,
即未再写入预置因子序列)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e9a0b1c2d3f4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None

_TABLE = "research_factor_series"
_COLUMN = "kind"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
