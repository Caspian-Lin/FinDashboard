"""data_backfill_511_item_fields

Revision ID: d3e4f5a6b7c8
Revises: c4a1b2d3e5f6
Create Date: 2026-11-09 04:55:00.000000

issue #511 复审修复:补数明细的冻结执行事实。

* ``missing_ranges`` —— 冻结的缺口区间(执行器只补这些区间,不重跑整窗);
* ``universe_filters`` —— 标的域超过冻结上限时的过滤器(发布面 = 请求面);
* ``repair_symbols`` —— 缓存质量修复的标的子集(来自既有发布的异常计数);
* item 唯一身份补 ``adjustment``:同一数据集可同时补 qfq 与 hqfq(或不同
  source),二者是不同的补数项,不能撞同一唯一键。

拆成独立 revision(而不是改 c4a1):已经应用过 c4a1 的本地库直接
``alembic upgrade head`` 即可补齐,不需要手动 downgrade。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c4a1b2d3e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_backfill_request_items",
        sa.Column("universe_filters", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "data_backfill_request_items",
        sa.Column("missing_ranges", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "data_backfill_request_items",
        sa.Column("repair_symbols", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.drop_constraint(
        "uq_backfill_item_identity",
        "data_backfill_request_items",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_backfill_item_identity",
        "data_backfill_request_items",
        ["request_id", "dataset", "source", "adjustment", "start_date", "end_date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_backfill_item_identity",
        "data_backfill_request_items",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_backfill_item_identity",
        "data_backfill_request_items",
        ["request_id", "dataset", "source", "start_date", "end_date"],
    )
    op.drop_column("data_backfill_request_items", "repair_symbols")
    op.drop_column("data_backfill_request_items", "missing_ranges")
    op.drop_column("data_backfill_request_items", "universe_filters")
