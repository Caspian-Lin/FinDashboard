"""background_jobs_phase_widen_348

Revision ID: d4e5f6a7b8c9
Revises: a7c3e9f1b2d4
Create Date: 2026-09-06 10:00:00.000000

issue #348:``background_jobs.phase`` VARCHAR(64) → VARCHAR(256)。

DB 实证(2026-09-05 23:59 data_sync 任务):收尾写完成语 phase
(「data_sync:done 标的 N 只;回填 … 仍缺失 …」,恒超 64 字符)触发
``StringDataRightTruncation``,实际工作(session.commit)已完成,任务却被
误报 failed。本迁移把列加宽至 256;配套 ``update_progress`` 写入前按同一
上限截断兜底(见 ``background_job_repo.py``),完成语本身同步收短。

回滚:downgrade 先把超长 phase 截回 64 字符再缩列,避免部署期间写入的
长完成语卡死回滚;截断只影响展示性标签,不丢任务状态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "background_jobs",
        "phase",
        existing_type=sa.String(length=64),
        type_=sa.String(length=256),
        existing_nullable=True,
    )


def downgrade() -> None:
    # 先截回旧列宽(部署新代码后可能已写入 >64 字符的 phase),再缩列,
    # 避免 StringDataRightTruncation 卡死回滚。
    op.execute("UPDATE background_jobs SET phase = LEFT(phase, 64) WHERE LENGTH(phase) > 64")
    op.alter_column(
        "background_jobs",
        "phase",
        existing_type=sa.String(length=256),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
