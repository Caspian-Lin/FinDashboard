"""research_strategy_specs_79

Revision ID: c29d8e3f0a79
Revises: a18c7d2e9f78
Create Date: 2026-07-29 20:00:00.000000

issue #79:无代码研究策略追加式版本历史。该表不引用账户、订单、成交或持仓。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c29d8e3f0a79"
down_revision = "a18c7d2e9f78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_strategy_specs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("strategy_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("change_type", sa.String(length=24), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "validation_errors",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("rollback_of_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "strategy_id",
            "version",
            name="uq_research_strategy_spec_version",
        ),
    )
    op.create_index(
        "ix_research_strategy_specs_strategy_id",
        "research_strategy_specs",
        ["strategy_id"],
    )
    op.create_index(
        "ix_research_strategy_specs_strategy_kind",
        "research_strategy_specs",
        ["strategy_kind"],
    )
    op.create_index(
        "ix_research_strategy_specs_status",
        "research_strategy_specs",
        ["status"],
    )
    op.create_index(
        "ix_research_strategy_specs_checksum",
        "research_strategy_specs",
        ["checksum"],
    )
    op.create_index(
        "ix_research_strategy_spec_history",
        "research_strategy_specs",
        ["strategy_id", "version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_strategy_spec_history",
        table_name="research_strategy_specs",
    )
    op.drop_index(
        "ix_research_strategy_specs_checksum",
        table_name="research_strategy_specs",
    )
    op.drop_index(
        "ix_research_strategy_specs_status",
        table_name="research_strategy_specs",
    )
    op.drop_index(
        "ix_research_strategy_specs_strategy_kind",
        table_name="research_strategy_specs",
    )
    op.drop_index(
        "ix_research_strategy_specs_strategy_id",
        table_name="research_strategy_specs",
    )
    op.drop_table("research_strategy_specs")
