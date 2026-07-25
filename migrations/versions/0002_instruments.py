"""instruments table

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26 00:40:00.000000

新增 instruments 表:全市场标的元数据(A 股 / ETF / 港股 / 美股)。
由 UniverseDiscovery 自动发现并 upsert,替代手工 symbols.yaml。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False, server_default=""),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("instrument_type", sa.String(16), nullable=False),
        sa.Column("exchange", sa.String(16), nullable=True),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("sector", sa.String(50), nullable=True),
        sa.Column("industry", sa.String(50), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_instruments_code", "instruments", ["code"], unique=True)
    op.create_index("ix_instruments_market", "instruments", ["market"])
    op.create_index("ix_instruments_type", "instruments", ["instrument_type"])
    op.create_index("ix_instruments_status", "instruments", ["status"])
    op.create_index(
        "ix_instruments_market_type", "instruments", ["market", "instrument_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_instruments_market_type", table_name="instruments")
    op.drop_index("ix_instruments_status", table_name="instruments")
    op.drop_index("ix_instruments_type", table_name="instruments")
    op.drop_index("ix_instruments_market", table_name="instruments")
    op.drop_index("ix_instruments_code", table_name="instruments")
    op.drop_table("instruments")
