"""research_dataset_releases_77

Revision ID: f17a6c4b9d77
Revises: e909c5d4e87c
Create Date: 2026-07-29 16:00:00.000000

issue #77: 发布时点安全的多资产研究数据集与覆盖审计。

新增不可变 ``research_dataset_releases`` 表,保存发布身份、覆盖质量、
能力清单、完整 manifest 和文件校验和。该表仅服务研究/回测,不触及交易表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f17a6c4b9d77"
down_revision = "e909c5d4e87c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_dataset_releases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_name", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("metadata_version", sa.String(length=32), nullable=False),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("coverage_pct", sa.Numeric(28, 8), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("quality_report", sa.JSON(), nullable=False),
        sa.Column("known_limitations", sa.JSON(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("release_checksum", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", name="uq_research_dataset_release_id"),
        sa.UniqueConstraint(
            "dataset_name",
            "source",
            "version",
            name="uq_research_dataset_release_version",
        ),
        sa.UniqueConstraint(
            "release_checksum",
            name="uq_research_dataset_release_checksum",
        ),
    )
    op.create_index(
        "ix_research_dataset_releases_release_id",
        "research_dataset_releases",
        ["release_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_dataset_releases_dataset_name",
        "research_dataset_releases",
        ["dataset_name"],
    )
    op.create_index(
        "ix_research_dataset_releases_source",
        "research_dataset_releases",
        ["source"],
    )
    op.create_index(
        "ix_research_dataset_releases_quality_status",
        "research_dataset_releases",
        ["quality_status"],
    )
    op.create_index(
        "ix_research_dataset_releases_release_checksum",
        "research_dataset_releases",
        ["release_checksum"],
        unique=True,
    )
    op.create_index(
        "ix_research_dataset_releases_published_at",
        "research_dataset_releases",
        ["published_at"],
    )
    op.create_index(
        "ix_research_dataset_release_lookup",
        "research_dataset_releases",
        ["dataset_name", "source", "quality_status", "published_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_dataset_release_lookup",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_published_at",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_release_checksum",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_quality_status",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_source",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_dataset_name",
        table_name="research_dataset_releases",
    )
    op.drop_index(
        "ix_research_dataset_releases_release_id",
        table_name="research_dataset_releases",
    )
    op.drop_table("research_dataset_releases")
