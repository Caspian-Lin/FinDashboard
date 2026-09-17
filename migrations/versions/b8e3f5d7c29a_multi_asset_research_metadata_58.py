"""multi_asset_research_metadata_58

Revision ID: b8e3f5d7c29a
Revises: 7a2c4d8e1b35
Create Date: 2026-07-28 14:00:00.000000

issue #58: 多资产研究数据与合约生命周期契约。

新增 7 张表:
* ``etf_metadata`` —— ETF 跟踪指数 / 类别 / 费率 / T+0;
* ``bond_metadata`` —— 国债 / 企业债 票息 / 到期 / 久期 / 信用;
* ``convertible_metadata`` —— 可转债 转股价 / 强赎 / 下修;
* ``futures_contracts`` —— 期货合约链(具体月份合约);
* ``continuous_futures_rules`` —— 连续期货拼接规则(可审计);
* ``instrument_lifecycle_events`` —— 时点化的公司行为 / 合约事件;
* ``dataset_manifests`` —— 数据集发布清单(覆盖率 / 校验和 / 质量门)。

修改:
* ``fills.market`` 新增可空列(回填现存行);
* ``positions.market`` 新增可空列(回填现存行)。

回滚:downgrade 删除新表 + 删除新列。``fills`` / ``positions`` 的 ``market`` 列
回滚后旧代码用 ``Market.A_SHARE`` 兜底,与 #58 前行为一致。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8e3f5d7c29a"
down_revision = "7a2c4d8e1b35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----------------------------------------------------------------- fills.market
    op.add_column(
        "fills",
        sa.Column("market", sa.String(length=16), nullable=True),
    )
    op.create_index("ix_fills_market", "fills", ["market"])
    # 回填现存行为 a_share(旧行为)
    op.execute("UPDATE fills SET market = 'a_share' WHERE market IS NULL")

    # ------------------------------------------------------------ positions.market
    op.add_column(
        "positions",
        sa.Column("market", sa.String(length=16), nullable=True),
    )
    op.create_index("ix_positions_market", "positions", ["market"])
    op.execute("UPDATE positions SET market = 'a_share' WHERE market IS NULL")

    # ----------------------------------------------------------------- etf_metadata
    op.create_table(
        "etf_metadata",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("fund_code", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("underlying_index", sa.String(length=32), nullable=True),
        sa.Column("underlying_asset_class", sa.String(length=16), nullable=False, server_default="equity"),
        sa.Column("management_fee_rate", sa.Numeric(28, 8), nullable=True),
        sa.Column("custody_fee_rate", sa.Numeric(28, 8), nullable=True),
        sa.Column("tracking_error", sa.Numeric(28, 8), nullable=True),
        sa.Column("inception_date", sa.Date(), nullable=True),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("delisting_date", sa.Date(), nullable=True),
        sa.Column("iopv_available", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("allows_t_plus_0", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dividend_policy", sa.String(length=16), nullable=False, server_default="cash"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("dataset_version", sa.String(length=128), nullable=False, server_default="v1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_etf_metadata_code"),
    )
    op.create_index("ix_etf_metadata_code", "etf_metadata", ["code"], unique=True)
    op.create_index("ix_etf_metadata_fund_code", "etf_metadata", ["fund_code"])
    op.create_index("ix_etf_metadata_category", "etf_metadata", ["category"])
    op.create_index("ix_etf_metadata_underlying_index", "etf_metadata", ["underlying_index"])

    # ----------------------------------------------------------------- bond_metadata
    op.create_table(
        "bond_metadata",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("face_value", sa.Numeric(20, 4), nullable=False, server_default="100"),
        sa.Column("coupon_rate", sa.Numeric(28, 8), nullable=True),
        sa.Column("coupon_frequency", sa.String(length=16), nullable=False, server_default="annual"),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=True),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("credit_rating", sa.String(length=16), nullable=True),
        sa.Column("credit_entity_type", sa.String(length=32), nullable=True),
        sa.Column("duration_years", sa.Numeric(28, 8), nullable=True),
        sa.Column("yield_to_maturity", sa.Numeric(28, 8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("dataset_version", sa.String(length=128), nullable=False, server_default="v1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_bond_metadata_code"),
    )
    op.create_index("ix_bond_metadata_code", "bond_metadata", ["code"], unique=True)
    op.create_index("ix_bond_metadata_maturity_date", "bond_metadata", ["maturity_date"])
    op.create_index("ix_bond_metadata_credit_entity_type", "bond_metadata", ["credit_entity_type"])

    # ------------------------------------------------------- convertible_metadata
    op.create_table(
        "convertible_metadata",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("underlying_stock_code", sa.String(length=20), nullable=False),
        sa.Column("conversion_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("conversion_ratio", sa.Numeric(28, 8), nullable=True),
        sa.Column("conversion_premium", sa.Numeric(28, 8), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=True),
        sa.Column(
            "coupon_schedule",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("redemption_yield", sa.Numeric(28, 8), nullable=True),
        sa.Column("forced_redeem_trigger", sa.Numeric(28, 8), nullable=True),
        sa.Column("put_back_trigger", sa.Numeric(28, 8), nullable=True),
        sa.Column("downward_revision_trigger", sa.Numeric(28, 8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("dataset_version", sa.String(length=128), nullable=False, server_default="v1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_convertible_metadata_code"),
    )
    op.create_index("ix_convertible_metadata_code", "convertible_metadata", ["code"], unique=True)
    op.create_index(
        "ix_convertible_metadata_underlying_stock_code",
        "convertible_metadata",
        ["underlying_stock_code"],
    )
    op.create_index(
        "ix_convertible_metadata_maturity_date",
        "convertible_metadata",
        ["maturity_date"],
    )

    # ------------------------------------------------------------- futures_contracts
    op.create_table(
        "futures_contracts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("contract_code", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=16), nullable=False),
        sa.Column("underlying_symbol", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("multiplier", sa.Numeric(20, 4), nullable=False),
        sa.Column("margin_rate", sa.Numeric(28, 8), nullable=False),
        sa.Column("price_limit_pct", sa.Numeric(28, 8), nullable=False),
        sa.Column("price_tick", sa.Numeric(20, 4), nullable=False),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("last_trade_date", sa.Date(), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("delivery_method", sa.String(length=16), nullable=False, server_default="cash"),
        sa.Column("settle_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("open_interest", sa.Numeric(20, 4), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("dataset_version", sa.String(length=128), nullable=False, server_default="v1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_code", name="uq_futures_contract_code"),
    )
    op.create_index(
        "ix_futures_contracts_contract_code",
        "futures_contracts",
        ["contract_code"],
        unique=True,
    )
    op.create_index("ix_futures_contracts_series_id", "futures_contracts", ["series_id"])
    op.create_index("ix_futures_contracts_exchange", "futures_contracts", ["exchange"])
    op.create_index("ix_futures_contracts_last_trade_date", "futures_contracts", ["last_trade_date"])
    op.create_index("ix_futures_series_exchange", "futures_contracts", ["series_id", "exchange"])

    # ------------------------------------------------------ continuous_futures_rules
    op.create_table(
        "continuous_futures_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("series_id", sa.String(length=16), nullable=False),
        sa.Column("roll_method", sa.String(length=16), nullable=False),
        sa.Column("adjustment_method", sa.String(length=16), nullable=False),
        sa.Column("roll_day_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("rule_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", name="uq_continuous_futures_rule_series_id"),
    )
    op.create_index(
        "ix_continuous_futures_rules_series_id",
        "continuous_futures_rules",
        ["series_id"],
        unique=True,
    )

    # ------------------------------------------------- instrument_lifecycle_events
    op.create_table(
        "instrument_lifecycle_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol",
            "event_type",
            "effective_date",
            "source",
            "dataset_version",
            name="uq_instrument_lifecycle_event",
        ),
    )
    op.create_index(
        "ix_instrument_lifecycle_events_symbol",
        "instrument_lifecycle_events",
        ["symbol"],
    )
    op.create_index(
        "ix_instrument_lifecycle_events_event_type",
        "instrument_lifecycle_events",
        ["event_type"],
    )
    op.create_index(
        "ix_instrument_lifecycle_events_effective_date",
        "instrument_lifecycle_events",
        ["effective_date"],
    )
    op.create_index(
        "ix_instrument_lifecycle_events_available_at",
        "instrument_lifecycle_events",
        ["available_at"],
    )
    op.create_index(
        "ix_lifecycle_symbol_effective",
        "instrument_lifecycle_events",
        ["symbol", "effective_date"],
    )
    op.create_index(
        "ix_lifecycle_effective_symbol",
        "instrument_lifecycle_events",
        ["effective_date", "symbol"],
    )

    # ----------------------------------------------------------------- dataset_manifests
    op.create_table(
        "dataset_manifests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_name", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbol_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "coverage_pct",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "gaps",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("checksum", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("quality_status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column(
            "quality_report",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("code_version", sa.String(length=64), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_name",
            "source",
            "version",
            name="uq_dataset_manifest",
        ),
    )
    op.create_index("ix_dataset_manifests_dataset_name", "dataset_manifests", ["dataset_name"])
    op.create_index("ix_dataset_manifests_checksum", "dataset_manifests", ["checksum"])
    op.create_index("ix_dataset_manifests_quality_status", "dataset_manifests", ["quality_status"])


def downgrade() -> None:
    # dataset_manifests
    op.drop_index("ix_dataset_manifests_quality_status", table_name="dataset_manifests")
    op.drop_index("ix_dataset_manifests_checksum", table_name="dataset_manifests")
    op.drop_index("ix_dataset_manifests_dataset_name", table_name="dataset_manifests")
    op.drop_table("dataset_manifests")

    # lifecycle_events
    op.drop_index("ix_lifecycle_effective_symbol", table_name="instrument_lifecycle_events")
    op.drop_index("ix_lifecycle_symbol_effective", table_name="instrument_lifecycle_events")
    op.drop_index(
        "ix_instrument_lifecycle_events_available_at",
        table_name="instrument_lifecycle_events",
    )
    op.drop_index(
        "ix_instrument_lifecycle_events_effective_date",
        table_name="instrument_lifecycle_events",
    )
    op.drop_index(
        "ix_instrument_lifecycle_events_event_type",
        table_name="instrument_lifecycle_events",
    )
    op.drop_index(
        "ix_instrument_lifecycle_events_symbol",
        table_name="instrument_lifecycle_events",
    )
    op.drop_table("instrument_lifecycle_events")

    # continuous_futures_rules
    op.drop_index(
        "ix_continuous_futures_rules_series_id",
        table_name="continuous_futures_rules",
    )
    op.drop_table("continuous_futures_rules")

    # futures_contracts
    op.drop_index("ix_futures_series_exchange", table_name="futures_contracts")
    op.drop_index("ix_futures_contracts_last_trade_date", table_name="futures_contracts")
    op.drop_index("ix_futures_contracts_exchange", table_name="futures_contracts")
    op.drop_index("ix_futures_contracts_series_id", table_name="futures_contracts")
    op.drop_index(
        "ix_futures_contracts_contract_code", table_name="futures_contracts"
    )
    op.drop_table("futures_contracts")

    # convertible_metadata
    op.drop_index(
        "ix_convertible_metadata_maturity_date",
        table_name="convertible_metadata",
    )
    op.drop_index(
        "ix_convertible_metadata_underlying_stock_code",
        table_name="convertible_metadata",
    )
    op.drop_index(
        "ix_convertible_metadata_code", table_name="convertible_metadata"
    )
    op.drop_table("convertible_metadata")

    # bond_metadata
    op.drop_index(
        "ix_bond_metadata_credit_entity_type", table_name="bond_metadata"
    )
    op.drop_index("ix_bond_metadata_maturity_date", table_name="bond_metadata")
    op.drop_index("ix_bond_metadata_code", table_name="bond_metadata")
    op.drop_table("bond_metadata")

    # etf_metadata
    op.drop_index("ix_etf_metadata_underlying_index", table_name="etf_metadata")
    op.drop_index("ix_etf_metadata_category", table_name="etf_metadata")
    op.drop_index("ix_etf_metadata_fund_code", table_name="etf_metadata")
    op.drop_index("ix_etf_metadata_code", table_name="etf_metadata")
    op.drop_table("etf_metadata")

    # fills.market / positions.market
    op.drop_index("ix_positions_market", table_name="positions")
    op.drop_column("positions", "market")
    op.drop_index("ix_fills_market", table_name="fills")
    op.drop_column("fills", "market")
