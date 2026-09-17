"""convertible_metadata_rating_265

Revision ID: a7c3e9f1b2d4
Revises: f0a1b2c3d4e5
Create Date: 2026-09-03 10:00:00.000000

issue #265:可转债数据链路 —— ``convertible_metadata`` 新增 ``rating`` 列。

tushare cb_basic 无评级字段;评级上游是 akshare ``bond_zh_cov`` 的债券评级列
(research_data_sync ``convertible_profiles`` 数据集回填)。可空、快照语义,
缺失保持 null 可见(#251 风格)。

回滚:downgrade 删除该列;旧代码忽略该字段,行为与 #265 前一致。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c3e9f1b2d4"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "convertible_metadata",
        sa.Column("rating", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("convertible_metadata", "rating")
