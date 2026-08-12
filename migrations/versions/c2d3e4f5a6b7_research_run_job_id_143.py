"""research_run_job_id_143

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-13 11:00:00.000000

issue #117 / #143:把 research-run 迁移到统一后台任务队列。

给 ``research_runs`` 加可空 ``job_id`` 列,关联 ``background_jobs.job_id``(字符串
引用,遵循 background_jobs「不建外键」约定)。queue 路由在同一事务内双写
research_runs + background_jobs,共用 idempotency_key 保证两系统幂等一致。

边界:不加外键(background_jobs 是独立调度表);job_id 不进 manifest JSON
(manifest checksum 是幂等基石,job_id 是服务端排队产物)。回滚方案为
``alembic downgrade -1`` + 关闭 worker 进程。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_runs",
        sa.Column("job_id", sa.String(length=48), nullable=True),
    )
    op.create_index(
        "ix_research_runs_job_id",
        "research_runs",
        ["job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_runs_job_id", table_name="research_runs")
    op.drop_column("research_runs", "job_id")
