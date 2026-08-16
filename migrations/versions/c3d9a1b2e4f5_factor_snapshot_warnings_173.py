"""factor_snapshot_warnings_173

Revision ID: c3d9a1b2e4f5
Revises: b8e9f0a1b2c3
Create Date: 2026-08-16 12:00:00.000000

issue #173:factor_snapshots 增加 warnings 列 —— bars/snapshot 输入模式下
数据集未发布 / instrument_profiles 缺失等降级提示随快照持久化,便于回看时
区分「数据完整」与「降级选股」。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d9a1b2e4f5"
down_revision = "b8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "factor_snapshots",
        sa.Column(
            "warnings",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("factor_snapshots", "warnings")
