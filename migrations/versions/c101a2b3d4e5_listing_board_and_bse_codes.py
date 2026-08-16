"""listing_board_and_bse_codes

Revision ID: c101a2b3d4e5
Revises: b9f0e1a2c3d4
Create Date: 2026-08-03 05:00:00.000000

issue #101:增加上市板块维度并修复 92 开头北交所代码。

只修改标的主数据和研究数据引用,不触及实盘订单、成交、持仓或风控表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c101a2b3d4e5"
down_revision = "b9f0e1a2c3d4"
branch_labels = None
depends_on = None


_RESEARCH_CODE_COLUMNS = (
    ("instrument_names", "instrument_code"),
    ("watchlist_items", "symbol_code"),
    ("research_instrument_profiles", "symbol"),
    ("research_daily_metrics", "symbol"),
    ("research_financial_indicators", "symbol"),
    ("research_industry_memberships", "symbol"),
    ("factor_values", "symbol"),
    ("instrument_lifecycle_events", "symbol"),
)


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("listing_board", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.create_index("ix_instruments_listing_board", "instruments", ["listing_board"])

    for table, column in _RESEARCH_CODE_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = regexp_replace({column}, '\\.SH$', '.BJ') "
                f"WHERE {column} ~ '^92[0-9]{{4}}\\.SH$'"
            )
        )
    op.execute(
        "UPDATE instruments SET code = regexp_replace(code, '\\.SH$', '.BJ'), "
        "exchange = 'BSE' WHERE code ~ '^92[0-9]{4}\\.SH$'"
    )
    op.execute(
        "UPDATE instruments SET listing_board = CASE "
        "WHEN exchange = 'BSE' OR code ~ '^(92|8|4)' THEN 'bse' "
        "WHEN code ~ '^689' "
        "OR substring(code from 1 for 6) BETWEEN '001001' AND '001199' "
        "OR substring(code from 1 for 6) BETWEEN '309800' AND '309999' THEN 'cdr' "
        "WHEN code ~ '^688' THEN 'star' "
        "WHEN code ~ '^30' THEN 'chinext' "
        "WHEN code ~ '^(600|601|603|605)' THEN 'sse_main' "
        "WHEN code ~ '^(000|001|002|003)' THEN 'szse_main' "
        "ELSE 'unknown' END "
        "WHERE instrument_type = 'stock' AND market = 'a_share'"
    )


def downgrade() -> None:
    # 92xxxx.BJ 是正确的规范代码,降级 schema 时不把已修复数据改回错误代码。
    op.drop_index("ix_instruments_listing_board", table_name="instruments")
    op.drop_column("instruments", "listing_board")
