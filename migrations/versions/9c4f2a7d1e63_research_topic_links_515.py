"""Research topic <-> ResearchRun links (#515).

Revision ID: 9c4f2a7d1e63
Revises: 0d2c51e14b6a
Create Date: 2026-11-09 22:00:00.000000

issue #515:从课题发起 ResearchRun 并自动关联任务与产物。

``research_topic_links`` 是课题与已有研究产物之间的**只追加**关联账本:

* 一行 = 一次「课题/目标版本 -> 产物」的精确引用,``entity_type`` 目前为
  ``research_run``(#516 复用同一表,不新增迁移);
* 链接创建后不可变 —— 不写 state/error 列。运行状态、任务进度与错误在读取时
  由 ``research_runs`` / ``background_jobs`` join 派生,worker 重启、租约重排
  与重复完成事件因此天然幂等(无回写即无重复事件可写);
* ``operation_id`` 部分唯一索引承载响应丢失后的幂等找回:同键重放返回原
  ``RR-`` / ``BJ-``,跨课题复用则 409;
* 无外键、无级联:与 #500 课题表一致,删除历史只需删表;不触碰实盘
  ``orders`` / ``fills`` / ``positions`` / ``audit_logs``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c4f2a7d1e63"
down_revision = "0d2c51e14b6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_topic_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("topic_id", sa.String(32), nullable=False),
        sa.Column("goal_version", sa.String(64), nullable=False),
        sa.Column("entry_id", sa.String(32), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(48), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "topic_id",
            "entity_type",
            "entity_id",
            name="uq_research_topic_links_topic_entity",
        ),
    )
    op.create_index(
        "uq_research_topic_links_operation",
        "research_topic_links",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("operation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_research_topic_links_entity",
        "research_topic_links",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_research_topic_links_topic_created",
        "research_topic_links",
        ["topic_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_topic_links_topic_created", table_name="research_topic_links"
    )
    op.drop_index(
        "ix_research_topic_links_entity", table_name="research_topic_links"
    )
    op.drop_index(
        "uq_research_topic_links_operation", table_name="research_topic_links"
    )
    op.drop_table("research_topic_links")
