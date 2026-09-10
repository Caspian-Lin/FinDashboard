"""trade_cal_futures_395

Revision ID: e6f7a8b9c0d1
Revises: d8e9f0a1b2c3
Create Date: 2026-09-09 12:00:00.000000

issue #395:期货交易日历落库(``fut_trade_cal``,CFFEX 行集)。

新表 ``trade_cal`` 按 ``exchange`` 存交易日:**与 #396 分支(A 股
SSE/SZSE 行集 + ``TradingCalendarStore`` DB 优先读路径)的建表迁移
schema 逐列一致** —— 同一张表靠 exchange 主键段区分市场。两分支并行
各自建表的协调约定:

* 本迁移带 ``has_table`` 守卫:表已存在(#396 迁移先到)时幂等跳过,
  不抛错;两迁移 schema 一致,先到者建表即满足双方。
* ``trade_cal`` 迁移撞号/双头是并行分支的已知协调点(#383 迁移文件头
  同款说明):合并时保留先合并者的迁移行,后合并者本迁移因守卫自然
  幂等;若 #396 后合并,建议其迁移同样加 ``has_table`` 守卫。

数据语义:tushare ``fut_trade_cal`` 返回窗口内全部日历日(含
``is_open=0`` 休市行);写入由 data_sync 执行器(#395)幂等 upsert,
``source='tushare'``。#396 的 akshare 回源只产 ``is_open=true`` 行,
两者共存互不干扰(行级 PK = (exchange, cal_date))。

回滚 = downgrade 删表(日历可由同步任务重建)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None

_TABLE = "trade_cal"


def _table_exists() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return bool(inspector.has_table(_TABLE))


def upgrade() -> None:
    if _table_exists():
        # #396 分支的同 schema 迁移先到:幂等跳过(schema 逐列一致)。
        return
    op.create_table(
        _TABLE,
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
        _TABLE,
        ["exchange", "is_open", "cal_date"],
    )


def downgrade() -> None:
    if not _table_exists():
        return
    op.drop_index("ix_trade_cal_open_date", table_name=_TABLE)
    op.drop_table(_TABLE)
