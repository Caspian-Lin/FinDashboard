"""background_jobs_117

Revision ID: b1c2d3e4f5a6
Revises: f7b2c3d4e5f6
Create Date: 2026-08-13 09:00:00.000000

issue #117 / #142:建立统一持久化后台任务队列的基础设施层。

新增 ``background_jobs`` 调度表 —— API 只创建任务(返回 202 + job_id),
独立 worker 进程用 PostgreSQL ``FOR UPDATE SKIP LOCKED`` 领取并执行。
本期不迁移任何真实业务,只用 echo kind 验证端到端跑通(业务迁移见 #143/#144)。

边界:本表与实盘 orders/fills/positions 完全隔离,``result_ref`` 只以字符串
引用产物(run_id / snapshot_id),不建任何外键。Worker payload 不含 broker 凭据 /
Tushare token / LLM API key;回滚方案为关闭 worker 进程 + 迁移 downgrade。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "f7b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=48), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("queue", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("progress_done", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=True),
        sa.Column("result_ref", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_background_jobs_job_id",
        "background_jobs",
        ["job_id"],
        unique=True,
    )
    op.create_index(
        "ix_background_jobs_kind", "background_jobs", ["kind"]
    )
    op.create_index(
        "ix_background_jobs_queue", "background_jobs", ["queue"]
    )
    op.create_index(
        "ix_background_jobs_status", "background_jobs", ["status"]
    )
    op.create_index(
        "ix_background_jobs_payload_checksum",
        "background_jobs",
        ["payload_checksum"],
    )
    op.create_index(
        "ix_background_jobs_created_at", "background_jobs", ["created_at"]
    )
    op.create_index(
        "ix_background_jobs_kind_status_created",
        "background_jobs",
        ["kind", "status", "created_at"],
    )
    op.create_index(
        "ix_background_jobs_status_lease",
        "background_jobs",
        ["status", "lease_until"],
    )
    op.create_index(
        "ix_background_jobs_queue_priority_created",
        "background_jobs",
        ["queue", "priority", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_background_jobs_queue_priority_created", table_name="background_jobs"
    )
    op.drop_index(
        "ix_background_jobs_status_lease", table_name="background_jobs"
    )
    op.drop_index(
        "ix_background_jobs_kind_status_created", table_name="background_jobs"
    )
    op.drop_index(
        "ix_background_jobs_created_at", table_name="background_jobs"
    )
    op.drop_index(
        "ix_background_jobs_payload_checksum", table_name="background_jobs"
    )
    op.drop_index("ix_background_jobs_status", table_name="background_jobs")
    op.drop_index("ix_background_jobs_queue", table_name="background_jobs")
    op.drop_index("ix_background_jobs_kind", table_name="background_jobs")
    op.drop_index(
        "ix_background_jobs_job_id", table_name="background_jobs"
    )
    op.drop_table("background_jobs")
