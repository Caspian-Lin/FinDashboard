"""research_code_promotion_219

Revision ID: f0a1b2c3d4e5
Revises: e7d8f9a0b1c2
Create Date: 2026-08-30 12:00:00.000000

issue #219:研究代码产物的机器晋级证据与 draft/active/retired 生命周期。
``status`` 保留原有生命周期语义,``promotion_status`` 单独表示 screen +
OOS 门结果;四向执行证据通过 artifact/run/research_run 引用与 JSON evidence
冻结,不触及实盘交易表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f0a1b2c3d4e5"
down_revision = "e7d8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_code_artifacts",
        sa.Column(
            "promotion_status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "research_code_artifacts",
        sa.Column("validation_experiment_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "research_code_artifacts",
        sa.Column("screen_run_id", sa.String(length=96), nullable=True),
    )
    op.add_column(
        "research_code_artifacts",
        sa.Column("promotion_evidence", sa.JSON(), nullable=True),
    )
    op.add_column(
        "research_code_artifacts",
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "research_code_artifacts",
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    # #215 旧登记逻辑直接写 active;把已有 active 行视为已存在的 legacy
    # 可信引用,避免升级后所有历史策略/因子突然从正式白名单消失。
    op.execute(
        sa.text(
            "UPDATE research_code_artifacts "
            "SET promotion_status='passed', promoted_at=COALESCE(promoted_at, updated_at) "
            "WHERE status='active'"
        )
    )
    op.create_index(
        "ix_research_code_artifacts_promotion_status",
        "research_code_artifacts",
        ["promotion_status"],
    )
    op.create_index(
        "ix_research_code_artifacts_validation_experiment_id",
        "research_code_artifacts",
        ["validation_experiment_id"],
    )
    op.create_index(
        "ix_research_code_artifacts_screen_run_id",
        "research_code_artifacts",
        ["screen_run_id"],
    )
    op.create_index(
        "ix_research_code_artifacts_kind_name_promotion",
        "research_code_artifacts",
        ["kind", "name", "promotion_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_code_artifacts_kind_name_promotion",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_screen_run_id",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_validation_experiment_id",
        table_name="research_code_artifacts",
    )
    op.drop_index(
        "ix_research_code_artifacts_promotion_status",
        table_name="research_code_artifacts",
    )
    op.drop_column("research_code_artifacts", "retired_at")
    op.drop_column("research_code_artifacts", "promoted_at")
    op.drop_column("research_code_artifacts", "promotion_evidence")
    op.drop_column("research_code_artifacts", "screen_run_id")
    op.drop_column("research_code_artifacts", "validation_experiment_id")
    op.drop_column("research_code_artifacts", "promotion_status")
