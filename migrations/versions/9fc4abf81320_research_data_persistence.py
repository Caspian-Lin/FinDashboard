"""research_data_persistence

Revision ID: 9fc4abf81320
Revises: 0004
Create Date: 2026-07-27 04:32:06.378560

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9fc4abf81320"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_sync_batches",
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=True),
        sa.Column("expected_rows", sa.Integer(), nullable=True),
        sa.Column("received_rows", sa.Integer(), nullable=False),
        sa.Column("accepted_rows", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), nullable=False),
        sa.Column("quality_report", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset", "source", "dataset_version", name="uq_research_batch_dataset_source_version"
        ),
    )
    op.create_index(
        "ix_research_batch_lookup",
        "research_sync_batches",
        ["dataset", "source", "status", "published_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_sync_batches_dataset"), "research_sync_batches", ["dataset"], unique=False
    )
    op.create_index(
        op.f("ix_research_sync_batches_published_at"),
        "research_sync_batches",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_sync_batches_source"), "research_sync_batches", ["source"], unique=False
    )
    op.create_index(
        op.f("ix_research_sync_batches_status"), "research_sync_batches", ["status"], unique=False
    )
    op.create_table(
        "research_daily_metrics",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column(
            "close", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "turnover_rate",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "turnover_rate_free",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "volume_ratio", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column("pe", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True),
        sa.Column(
            "pe_ttm", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column("pb", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True),
        sa.Column("ps", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True),
        sa.Column(
            "ps_ttm", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "dividend_yield",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "dividend_yield_ttm",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "total_shares", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "float_shares", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "free_shares", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "total_market_cap",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "circulating_market_cap",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column("limit_status", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["research_sync_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            "trade_date",
            name="uq_research_daily_source_version_symbol_date",
        ),
    )
    op.create_index(
        "ix_research_daily_date_symbol",
        "research_daily_metrics",
        ["trade_date", "symbol"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_daily_metrics_available_at"),
        "research_daily_metrics",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_daily_metrics_batch_id"),
        "research_daily_metrics",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_daily_symbol_date",
        "research_daily_metrics",
        ["symbol", "trade_date"],
        unique=False,
    )
    op.create_table(
        "research_financial_indicators",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("announcement_date", sa.Date(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("update_flag", sa.String(length=16), nullable=False),
        sa.Column("eps", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True),
        sa.Column(
            "diluted_eps", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "book_value_per_share",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "operating_cash_flow_per_share",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "return_on_equity",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "weighted_return_on_equity",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "gross_profit_margin",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "net_profit_margin",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "debt_to_assets",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "revenue_yoy", sa.Numeric(precision=28, scale=8, decimal_return_scale=8), nullable=True
        ),
        sa.Column(
            "net_profit_yoy",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column(
            "operating_cash_flow_yoy",
            sa.Numeric(precision=28, scale=8, decimal_return_scale=8),
            nullable=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["research_sync_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            "report_period",
            "announcement_date",
            "update_flag",
            name="uq_research_financial_revision",
        ),
    )
    op.create_index(
        op.f("ix_research_financial_indicators_available_at"),
        "research_financial_indicators",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_financial_indicators_batch_id"),
        "research_financial_indicators",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_financial_period_symbol",
        "research_financial_indicators",
        ["report_period", "symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_financial_symbol_period",
        "research_financial_indicators",
        ["symbol", "report_period"],
        unique=False,
    )
    op.create_table(
        "research_industry_classifications",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("taxonomy", sa.String(length=32), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("industry_code", sa.String(length=32), nullable=False),
        sa.Column("industry_name", sa.String(length=100), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["research_sync_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "dataset_version",
            "taxonomy",
            "level",
            "industry_code",
            name="uq_research_industry_classification",
        ),
    )
    op.create_index(
        op.f("ix_research_industry_classifications_batch_id"),
        "research_industry_classifications",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_industry_taxonomy_level",
        "research_industry_classifications",
        ["taxonomy", "level", "industry_code"],
        unique=False,
    )
    op.create_table(
        "research_industry_memberships",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("taxonomy", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("security_name", sa.String(length=100), nullable=False),
        sa.Column("level1_code", sa.String(length=32), nullable=False),
        sa.Column("level2_code", sa.String(length=32), nullable=False),
        sa.Column("level3_code", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["research_sync_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "dataset_version",
            "taxonomy",
            "symbol",
            "level3_code",
            "valid_from",
            name="uq_research_industry_membership",
        ),
    )
    op.create_index(
        op.f("ix_research_industry_memberships_available_at"),
        "research_industry_memberships",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_industry_memberships_batch_id"),
        "research_industry_memberships",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_membership_symbol_valid",
        "research_industry_memberships",
        ["symbol", "valid_from", "valid_to"],
        unique=False,
    )
    op.create_index(
        "ix_research_membership_valid_symbol",
        "research_industry_memberships",
        ["valid_from", "valid_to", "symbol"],
        unique=False,
    )
    op.create_table(
        "research_instrument_profiles",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("list_status", sa.String(length=8), nullable=False),
        sa.Column("list_date", sa.Date(), nullable=False),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("industry", sa.String(length=100), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["research_sync_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "dataset_version", "symbol", name="uq_research_profile_source_version_symbol"
        ),
    )
    op.create_index(
        op.f("ix_research_instrument_profiles_available_at"),
        "research_instrument_profiles",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_instrument_profiles_batch_id"),
        "research_instrument_profiles",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_profile_date_symbol",
        "research_instrument_profiles",
        ["list_date", "symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_profile_symbol_date",
        "research_instrument_profiles",
        ["symbol", "list_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_profile_symbol_date", table_name="research_instrument_profiles")
    op.drop_index("ix_research_profile_date_symbol", table_name="research_instrument_profiles")
    op.drop_index(
        op.f("ix_research_instrument_profiles_batch_id"), table_name="research_instrument_profiles"
    )
    op.drop_index(
        op.f("ix_research_instrument_profiles_available_at"),
        table_name="research_instrument_profiles",
    )
    op.drop_table("research_instrument_profiles")
    op.drop_index("ix_research_membership_valid_symbol", table_name="research_industry_memberships")
    op.drop_index("ix_research_membership_symbol_valid", table_name="research_industry_memberships")
    op.drop_index(
        op.f("ix_research_industry_memberships_batch_id"),
        table_name="research_industry_memberships",
    )
    op.drop_index(
        op.f("ix_research_industry_memberships_available_at"),
        table_name="research_industry_memberships",
    )
    op.drop_table("research_industry_memberships")
    op.drop_index(
        "ix_research_industry_taxonomy_level", table_name="research_industry_classifications"
    )
    op.drop_index(
        op.f("ix_research_industry_classifications_batch_id"),
        table_name="research_industry_classifications",
    )
    op.drop_table("research_industry_classifications")
    op.drop_index("ix_research_financial_symbol_period", table_name="research_financial_indicators")
    op.drop_index("ix_research_financial_period_symbol", table_name="research_financial_indicators")
    op.drop_index(
        op.f("ix_research_financial_indicators_batch_id"),
        table_name="research_financial_indicators",
    )
    op.drop_index(
        op.f("ix_research_financial_indicators_available_at"),
        table_name="research_financial_indicators",
    )
    op.drop_table("research_financial_indicators")
    op.drop_index("ix_research_daily_symbol_date", table_name="research_daily_metrics")
    op.drop_index(op.f("ix_research_daily_metrics_batch_id"), table_name="research_daily_metrics")
    op.drop_index(
        op.f("ix_research_daily_metrics_available_at"), table_name="research_daily_metrics"
    )
    op.drop_index("ix_research_daily_date_symbol", table_name="research_daily_metrics")
    op.drop_table("research_daily_metrics")
    op.drop_index(op.f("ix_research_sync_batches_status"), table_name="research_sync_batches")
    op.drop_index(op.f("ix_research_sync_batches_source"), table_name="research_sync_batches")
    op.drop_index(op.f("ix_research_sync_batches_published_at"), table_name="research_sync_batches")
    op.drop_index(op.f("ix_research_sync_batches_dataset"), table_name="research_sync_batches")
    op.drop_index("ix_research_batch_lookup", table_name="research_sync_batches")
    op.drop_table("research_sync_batches")
