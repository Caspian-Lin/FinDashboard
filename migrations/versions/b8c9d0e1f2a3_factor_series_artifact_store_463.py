"""factor_series_artifact_store_463

Revision ID: b8c9d0e1f2a3
Revises: 765d46a5f29e
Create Date: 2026-09-13 12:00:00.000000

issue #463(用户拍板「parquet + checksum 工件」):因子序列 values 从
JSONB 行内改存 canonical parquet 工件。``research_factor_series`` 新增
``artifact_relpath``(NULL = 行内 JSONB 旧行,checksum 语义不变);
``values`` 转可空(工件行 values 为 NULL,content_checksum = 工件文件
sha256)。``dates`` 保持行内(coverage 检查/summary 不触工件文件)。

纯研究域存储;旧行零迁移(照常按行内模式读写),新写入路径才落工件。
回滚 = downgrade(列删除;已落工件行会失去 relpath —— downgrade 前应
确认无工件行或接受其只读失效,工件文件本身保留可重建)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "765d46a5f29e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_factor_series",
        sa.Column("artifact_relpath", sa.String(length=260), nullable=True),
    )
    op.alter_column("research_factor_series", "values", nullable=True)


def downgrade() -> None:
    # 有工件行时 values 为 NULL,回填空对象以过 NOT NULL(内容经工件仍在,
    # 但行内模式读不到 —— downgrade 属破坏性操作,生产先确认无工件行)。
    op.execute(
        "UPDATE research_factor_series SET values = '{}'::jsonb "
        "WHERE values IS NULL AND artifact_relpath IS NOT NULL"
    )
    op.alter_column("research_factor_series", "values", nullable=False)
    op.drop_column("research_factor_series", "artifact_relpath")
