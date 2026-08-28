"""background_jobs_archive_221

Revision ID: b9c0d1e2f3a4
Revises: c4d5e6f7a8b9
Create Date: 2026-08-29 10:00:00.000000

issue #221:后台任务归档 —— ``background_jobs`` 新增 ``archived_at`` 可空时间列。

归档是独立于 status 的展示维度(不新增状态枚举值,不改状态机):归档后从
默认列表(``archived=exclude``)隐藏但**不删除**,单查接口始终可达,可取消归档。
仅终态任务(succeeded/failed/cancelled/interrupted)允许归档;worker 维护路径
(``requeue_due``)跳过已归档行,归档即冻结、不会被自动重排回队列。
downgrade 直接删列,未归档语义不受影响。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "background_jobs",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("background_jobs", "archived_at")
