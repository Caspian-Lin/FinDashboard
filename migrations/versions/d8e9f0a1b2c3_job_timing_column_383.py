"""job_timing_column_383

Revision ID: d8e9f0a1b2c3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-08 12:00:00.000000

issue #383:所有 job kind 通用的性能计时落点。

``background_jobs`` 新增 nullable JSON 列 ``timing``,worker 统一包裹
``_execute_with_heart`` 后写入 ``{"execute_elapsed_seconds",
"parquet_reads": {read_ops, read_elapsed_ms, read_bytes, ops_by_entry}}``
——回答「慢在 IO 还是计算」(parquet 读耗时占 wall-clock 的比例)。
此前只有 backtest_run / research_run 两个 kind 在各自引擎内手写了段级
计时(#285),其余 kind 无任何计时;且引擎内激活会遮蔽外层聚合,层 1 在
worker 全程激活后靠 #383 的嵌套句柄栈语义(外层=全程)互补。

列可空:历史行与未走到收口的行保持 NULL,不设 server_default,
读侧(job_get / JobOut)对 None 自然兼容。

回滚 = downgrade 删列(数据即丢,可观测性数据无保留价值)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None

_TABLE = "background_jobs"
_COLUMN = "timing"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
