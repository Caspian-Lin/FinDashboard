"""research_dataset_release_instruments_509

Revision ID: 9c1d2e3f4a50
Revises: 62f8b1e7a905
Create Date: 2026-11-09 02:30:00.000000

issue #509: 冻结发布的逐标的覆盖派生读模型。表只服务目录分页、按标的反查
与标的集合包含判断;``research_dataset_releases.manifest`` 仍是唯一事实源。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c1d2e3f4a50"
down_revision = "62f8b1e7a905"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_dataset_release_instruments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("instrument_type", sa.String(length=32), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("listing_board", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("expected_sessions", sa.Integer(), nullable=False),
        sa.Column("missing_sessions", sa.Integer(), nullable=False),
        sa.Column("suspended_sessions", sa.Integer(), nullable=False),
        sa.Column("anomaly_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("coverage_pct", sa.Numeric(28, 8), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["research_dataset_releases.release_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id", "code", name="uq_release_instrument_code"
        ),
    )
    op.create_index(
        "ix_research_dataset_release_instruments_release_id",
        "research_dataset_release_instruments",
        ["release_id"],
    )
    op.create_index(
        "ix_release_instrument_code",
        "research_dataset_release_instruments",
        ["code", "release_id"],
    )
    op.create_index(
        "ix_release_instrument_type",
        "research_dataset_release_instruments",
        ["release_id", "instrument_type", "ready"],
    )
    op.create_index(
        "ix_release_instrument_missing",
        "research_dataset_release_instruments",
        ["release_id", "missing_sessions"],
    )


def downgrade() -> None:
    op.drop_table("research_dataset_release_instruments")
