"""research_code_artifacts_215

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-08-29 12:00:00.000000

issue #215:研究代码产物登记表。agent 通过 MCP 提交的策略/因子代码在
bare git 仓库(settings research_code_repo_path)中版本化,本表登记每次
提交的 (kind, name, commit, checksum, status) 引用与生命周期
(active/retired/draft)。纯研究域存储,不执行代码、不触实盘表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_code_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("commit", sa.String(length=40), nullable=False),
        sa.Column("path", sa.String(length=160), nullable=False),
        sa.Column("checksum", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_code_artifacts_artifact_id",
        "research_code_artifacts",
        ["artifact_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_code_artifacts_kind",
        "research_code_artifacts",
        ["kind"],
    )
    op.create_index(
        "ix_research_code_artifacts_name",
        "research_code_artifacts",
        ["name"],
    )
    op.create_index(
        "ix_research_code_artifacts_status",
        "research_code_artifacts",
        ["status"],
    )
    op.create_index(
        "ix_research_code_artifacts_created_at",
        "research_code_artifacts",
        ["created_at"],
    )
    op.create_index(
        "ix_research_code_artifacts_kind_name_status",
        "research_code_artifacts",
        ["kind", "name", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_code_artifacts_kind_name_status",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_created_at",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_status",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_name",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_kind",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_artifact_id",
        table_name="research_code_artifacts",
    )
    op.drop_table("research_code_artifacts")
