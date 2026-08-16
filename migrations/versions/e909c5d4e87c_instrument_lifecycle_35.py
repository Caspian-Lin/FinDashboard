"""instrument_lifecycle_35

Revision ID: e909c5d4e87c
Revises: b8e3f5d7c29a
Create Date: 2026-07-29 10:00:00.000000

issue #35: 标的生命周期检测 —— 退市标记 + 停牌检测 + 改名追踪。

新增 / 修改:
* 新表 ``instrument_names`` —— 标的名称历史(valid_from / valid_to 区间),改名追踪;
* ``instruments`` 新增 ``missing_runs`` 列(默认 0)—— 退市二次确认的连续消失计数。

回滚:downgrade 删除 ``instrument_names`` 表 + 删除 ``missing_runs`` 列。
``missing_runs`` 回滚后旧代码忽略该字段,行为与 #35 前一致(纯 upsert)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e909c5d4e87c"
down_revision = "b8e3f5d7c29a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ instrument_names
    op.create_table(
        "instrument_names",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_instrument_names_instrument_code",
        "instrument_names",
        ["instrument_code"],
    )
    op.create_index(
        "ix_instrument_names_code_valid",
        "instrument_names",
        ["instrument_code", "valid_from"],
    )

    # ------------------------------------------------------------------ instruments.missing_runs
    op.add_column(
        "instruments",
        sa.Column("missing_runs", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # instruments.missing_runs
    op.drop_column("instruments", "missing_runs")

    # instrument_names
    op.drop_index("ix_instrument_names_code_valid", table_name="instrument_names")
    op.drop_index(
        "ix_instrument_names_instrument_code", table_name="instrument_names"
    )
    op.drop_table("instrument_names")
