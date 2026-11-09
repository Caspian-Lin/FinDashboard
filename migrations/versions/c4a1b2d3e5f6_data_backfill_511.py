"""data_backfill_511

Revision ID: c4a1b2d3e5f6
Revises: 9c1d2e3f4a50
Create Date: 2026-11-09 03:10:00.000000

issue #511:课题缺口 → 全局补数请求。

* ``data_backfill_requests`` —— 请求头:冻结的 spec/plan、幂等键、
  fingerprint、状态机、复用与重试血缘、job 与产出发布;
* ``data_backfill_request_items`` —— 逐数据集明细:动作/窗口/冻结标的
  (或过滤器)/冻结的缺口区间/估算/批次与发布产物/逐项错误。

item 唯一身份含 ``adjustment``:同一数据集可同时补 qfq 与 hqfq(或不同
source),二者是不同的补数项,不能撞同一唯一键。

幂等键唯一性用**部分唯一索引**只约束活跃行(与 ``background_jobs`` #371
同语义):``failed`` / ``cancelled`` 是状态机永不回访的终态,放行同键重建,
等价重试不会被失败尸体以 conflict 挡死;``succeeded`` / ``partial`` /
``blocked`` 保持唯一(幂等命中)。

纯研究/数据域表,不建任何指向实盘表的外键;``job_id`` / ``release_ids``
只做字符串引用。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4a1b2d3e5f6"
down_revision = "9c1d2e3f4a50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_backfill_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scope_checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("reused_request_ids", sa.JSON(), nullable=False),
        sa.Column("retry_of_request_id", sa.String(length=32), nullable=True),
        sa.Column("job_id", sa.String(length=48), nullable=True),
        sa.Column("release_ids", sa.JSON(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_data_backfill_request_id"),
    )
    op.create_index(
        "ix_data_backfill_requests_request_id",
        "data_backfill_requests",
        ["request_id"],
    )
    op.create_index(
        "ix_data_backfill_requests_idempotency_key",
        "data_backfill_requests",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_data_backfill_requests_status",
        "data_backfill_requests",
        ["status"],
    )
    op.create_index(
        "ix_data_backfill_requests_created_at",
        "data_backfill_requests",
        ["created_at"],
    )
    # 幂等唯一性只约束活跃行(#371 先例):failed/cancelled 放行同键重建。
    op.create_index(
        "uq_backfill_idempotency_active",
        "data_backfill_requests",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('failed', 'cancelled')"),
    )
    op.create_index(
        "ix_backfill_fingerprint",
        "data_backfill_requests",
        ["fingerprint", "status"],
    )
    op.create_index(
        "ix_backfill_status_created",
        "data_backfill_requests",
        ["status", "created_at"],
    )

    op.create_table(
        "data_backfill_request_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("estimated_requests", sa.Integer(), nullable=False),
        sa.Column("batch_ids", sa.JSON(), nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=True),
        sa.Column("accepted_rows", sa.BigInteger(), nullable=False),
        sa.Column("rejected_rows", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["data_backfill_requests.request_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "dataset",
            "source",
            "start_date",
            "end_date",
            name="uq_backfill_item_identity",
        ),
    )
    op.create_index(
        "ix_data_backfill_request_items_request_id",
        "data_backfill_request_items",
        ["request_id"],
    )
    op.create_index(
        "ix_data_backfill_request_items_status",
        "data_backfill_request_items",
        ["status"],
    )
    op.create_index(
        "ix_backfill_item_dataset",
        "data_backfill_request_items",
        ["dataset", "source", "status", "end_date"],
    )


def downgrade() -> None:
    op.drop_table("data_backfill_request_items")
    op.drop_table("data_backfill_requests")
