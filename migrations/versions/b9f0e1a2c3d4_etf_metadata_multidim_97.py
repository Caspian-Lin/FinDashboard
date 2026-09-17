"""etf_metadata_multidim_97

Revision ID: b9f0e1a2c3d4
Revises: a7b8c9d1e2f3
Create Date: 2026-07-31 10:00:00.000000

issue #97:ETF 元数据多维分类扩展。

* etf_metadata 表新增 execution_profile / underlying_market / strategy_type
  正交维度 + 溯源(source_updated_at / rule_version / confidence /
  review_status / evidence / raw_payload)和人工覆盖标志(manual_override)。
* 新增 etf_metadata_audits 表记录人工覆盖流水(操作者 / 时间 / 理由 / 前后值)。
* 既有 category 列保留不动,兼容旧发布链路;新数据由分类器自动填充
  execution_profile 后派生 category。

本迁移只修改研究数据表, 不触及实盘 orders/fills/positions。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9f0e1a2c3d4"
down_revision = "a7b8c9d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "etf_metadata",
        sa.Column("execution_profile", sa.String(32), nullable=True),
    )
    op.add_column(
        "etf_metadata",
        sa.Column("underlying_market", sa.String(16), nullable=False, server_default="domestic"),
    )
    op.add_column(
        "etf_metadata",
        sa.Column("strategy_type", sa.String(16), nullable=False, server_default="index"),
    )
    op.add_column(
        "etf_metadata",
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "etf_metadata",
        sa.Column("rule_version", sa.String(32), nullable=False, server_default=""),
    )
    op.add_column(
        "etf_metadata",
        sa.Column(
            "confidence",
            sa.Numeric(28, 8, decimal_return_scale=8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "etf_metadata",
        sa.Column(
            "review_status", sa.String(32), nullable=False, server_default="needs_review"
        ),
    )
    op.add_column(
        "etf_metadata",
        sa.Column(
            "evidence",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "etf_metadata",
        sa.Column(
            "manual_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "etf_metadata",
        sa.Column(
            "raw_payload",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.create_index(
        "ix_etf_metadata_execution_profile",
        "etf_metadata",
        ["execution_profile"],
    )
    op.create_index(
        "ix_etf_metadata_review_status",
        "etf_metadata",
        ["review_status"],
    )

    # 数据回填:从旧 category 派生 execution_profile;现有记录均视为人工确认,
    # manual_override=true 保护其不被后续自动同步覆盖。
    op.execute(
        sa.text(
            """
            UPDATE etf_metadata SET
                execution_profile = CASE category
                    WHEN 'equity' THEN 'domestic_equity_etf'
                    WHEN 'index' THEN 'domestic_equity_etf'
                    WHEN 'cross_border' THEN 'cross_border_etf'
                    WHEN 'bond' THEN 'bond_etf'
                    WHEN 'money_market' THEN 'money_market_etf'
                    WHEN 'commodity' THEN 'commodity_etf'
                END,
                underlying_market = CASE
                    WHEN category = 'cross_border' THEN 'overseas'
                    ELSE 'domestic'
                END,
                review_status = 'manually_confirmed',
                manual_override = true,
                confidence = 1.0
            WHERE execution_profile IS NULL
            """
        )
    )

    op.create_table(
        "etf_metadata_audits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("field_name", sa.String(32), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_etf_audit_code_changed",
        "etf_metadata_audits",
        ["code", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_etf_audit_code_changed", table_name="etf_metadata_audits")
    op.drop_table("etf_metadata_audits")
    op.drop_index("ix_etf_metadata_review_status", table_name="etf_metadata")
    op.drop_index("ix_etf_metadata_execution_profile", table_name="etf_metadata")
    op.drop_column("etf_metadata", "raw_payload")
    op.drop_column("etf_metadata", "manual_override")
    op.drop_column("etf_metadata", "evidence")
    op.drop_column("etf_metadata", "review_status")
    op.drop_column("etf_metadata", "confidence")
    op.drop_column("etf_metadata", "rule_version")
    op.drop_column("etf_metadata", "source_updated_at")
    op.drop_column("etf_metadata", "strategy_type")
    op.drop_column("etf_metadata", "underlying_market")
    op.drop_column("etf_metadata", "execution_profile")
