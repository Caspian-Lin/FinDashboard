"""job_idempotency_dead_recreate_371

Revision ID: b7c8d9e0f1a2
Revises: a9b0c1d2e3f4
Create Date: 2026-09-07 22:00:00.000000

issue #371:幂等键命中 failed/cancelled 死行时放行同键新建。

此前 ``background_jobs.idempotency_key`` 是无条件 UNIQUE:同参数重试在任务
失败后永远命中失败尸体(要么返回旧失败记录,要么 conflict),修复后的
代码无法重新入队——factor_series_build(BJ-7DA144 事故)首当其冲。

迁移把唯一性收敛为**部分唯一索引**:只约束「活跃」行(status NOT IN
('failed','cancelled'))。succeeded 保持唯一(幂等命中 = 内容寻址缓存
命中);interrupted 保持唯一(#305 replay / lease 回收重排依赖单行语义);
failed/cancelled 是状态机永不回访的死行,不挡新建。

回滚 = downgrade 恢复列级 UNIQUE。注意:若库中已存在同键多行(本迁移
应用后产生),downgrade 会因唯一约束失败——先手工清理死行重复。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None

_TABLE = "background_jobs"
_INDEX = "uq_background_jobs_idempotency_active"
# 与 finboard_shared.background_jobs.BackgroundJobStatus 的死状态字面量一致;
# 迁移文件不 import 应用代码(既有约定)。
_DEAD_STATUSES = ("failed", "cancelled")


def upgrade() -> None:
    # 列级 UNIQUE 是建表迁移(b1c2d3e4f5a6)里的 UniqueConstraint,无名,
    # PostgreSQL 里自动命名为 background_jobs_idempotency_key_key。
    op.drop_constraint(
        "background_jobs_idempotency_key_key", _TABLE, type_="unique"
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "status NOT IN ('failed', 'cancelled')"
        ),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_unique_constraint(
        "background_jobs_idempotency_key_key", _TABLE, ["idempotency_key"]
    )
