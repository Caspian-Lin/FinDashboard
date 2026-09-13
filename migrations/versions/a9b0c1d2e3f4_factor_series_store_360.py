"""factor_series_store_360

Revision ID: a9b0c1d2e3f4
Revises: d4e5f6a7b8c9
Create Date: 2026-09-07 10:00:00.000000

issue #360:内容寻址的因子序列工件存储 ``research_factor_series``。

因子序列从「用户策划的点快照」(``factor_feature_snapshots`` 绑定单一
decision_at x 单一 bars 发布)改为派生工件:``series_key`` 按
(代码 commit, bars 主发布, 研究发布联合集, params, 窗口) 内容寻址,
``values`` 为逐决策日截面;换 bars 发布走托管批量重建(入队 N 个既有
build job),不再全作废 + 手工重跑 RCR。

纯研究域存储,不迁移历史快照,不触实盘表。回滚 = downgrade 整表删除
(工件可由 build job 按内容寻址键确定性重建)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a9b0c1d2e3f4"
# 整合:#359 的 RCR mode 迁移先落(b5e6f7a8c9d0),本迁移串接其后(单头)
down_revision = "b5e6f7a8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_factor_series",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("series_id", sa.String(length=20), nullable=False),
        sa.Column("series_key", sa.String(length=64), nullable=False),
        sa.Column("code_artifact", sa.String(length=64), nullable=False),
        sa.Column("code_commit", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_release_ids", JSONB(), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("dates", JSONB(), nullable=False),
        sa.Column("values", JSONB(), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("quality", JSONB(), nullable=True),
        sa.Column("source_run_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_factor_series")),
    )
    op.create_index(
        op.f("ix_research_factor_series_series_id"),
        "research_factor_series",
        ["series_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_research_factor_series_series_key"),
        "research_factor_series",
        ["series_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_research_factor_series_code_artifact"),
        "research_factor_series",
        ["code_artifact"],
    )
    op.create_index(
        op.f("ix_research_factor_series_release_id"),
        "research_factor_series",
        ["release_id"],
    )
    op.create_index(
        "ix_research_factor_series_release_window",
        "research_factor_series",
        ["release_id", "window_start", "window_end"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_factor_series_release_window",
        table_name="research_factor_series",
    )
    op.drop_index(
        op.f("ix_research_factor_series_release_id"),
        table_name="research_factor_series",
    )
    op.drop_index(
        op.f("ix_research_factor_series_code_artifact"),
        table_name="research_factor_series",
    )
    op.drop_index(
        op.f("ix_research_factor_series_series_key"),
        table_name="research_factor_series",
    )
    op.drop_index(
        op.f("ix_research_factor_series_series_id"),
        table_name="research_factor_series",
    )
    op.drop_table("research_factor_series")
