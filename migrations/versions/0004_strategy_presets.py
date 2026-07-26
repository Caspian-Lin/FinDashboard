"""strategy presets

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-27 02:30:00.000000

新增 strategy_presets 表,仅保存经过内置策略 schema 校验的参数。
不保存、导入或执行用户代码。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_presets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_strategy_presets_name",
        "strategy_presets",
        ["name"],
        unique=True,
    )
    op.create_index(
        "ix_strategy_presets_strategy",
        "strategy_presets",
        ["strategy"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_presets_strategy", table_name="strategy_presets")
    op.drop_index("ix_strategy_presets_name", table_name="strategy_presets")
    op.drop_table("strategy_presets")
