"""trade_cal_and_research_suspensions_396

Revision ID: e5f6a7b8c9d0
Revises: d8e9f0a1b2c3
Create Date: 2026-09-09 12:00:00.000000

issue #396:交易日历落库 + suspend_d 停复牌研究数据集。

两个新表:

* ``trade_cal`` —— A 股交易日历(按 exchange 存交易日,幂等 upsert)。
  读取路径改「DB 优先,缺失回源 akshare 并回写」后,发布覆盖率审计 /
  #334 并集日历消费点 / 启动期不再依赖 akshare 可用性。``is_open`` 列
  预留非交易日行(tushare ``trade_cal`` 口径);akshare 回源只产生
  ``is_open=true`` 行。
* ``research_suspensions`` —— suspend_d(tushare doc_id=214,按日全市场
  停复牌枚举)研究数据集落点,批次发布语义(挂
  ``research_sync_batches.id``)。停牌是**交易状态**不是条款事件,独立
  表、不入 ``instrument_lifecycle_events``(与姊妹 PR 的 bulk_download
  缓存侧口径并存,不互相替代)。

回滚 = downgrade 删两表(可由同步任务重建,无不可再生数据)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_cal",
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("cal_date", sa.Date(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("exchange", "cal_date", name="pk_trade_cal"),
    )
    op.create_index(
        "ix_trade_cal_open_date",
        "trade_cal",
        ["exchange", "is_open", "cal_date"],
    )
    op.create_table(
        "research_suspensions",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("suspend_type", sa.String(length=8), nullable=False),
        sa.Column("suspend_timing", sa.String(length=32), nullable=True),
        sa.Column("suspend_kind", sa.String(length=24), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["research_sync_batches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            "trade_date",
            name="uq_research_suspension_source_version_symbol_date",
        ),
    )
    op.create_index(
        "ix_research_suspension_symbol_date",
        "research_suspensions",
        ["symbol", "trade_date"],
    )
    op.create_index(
        "ix_research_suspension_date_symbol",
        "research_suspensions",
        ["trade_date", "symbol"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_suspension_date_symbol", table_name="research_suspensions"
    )
    op.drop_index(
        "ix_research_suspension_symbol_date", table_name="research_suspensions"
    )
    op.drop_table("research_suspensions")
    op.drop_index("ix_trade_cal_open_date", table_name="trade_cal")
    op.drop_table("trade_cal")
