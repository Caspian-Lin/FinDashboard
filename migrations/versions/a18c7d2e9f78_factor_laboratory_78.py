"""factor_laboratory_78

Revision ID: a18c7d2e9f78
Revises: f17a6c4b9d77
Create Date: 2026-07-29 18:00:00.000000

issue #78: 版本化 FeatureSnapshot、FactorSignal 与因子实验审计。
所有表仅服务离线研究;不引用账户、订单、成交或持仓表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a18c7d2e9f78"
down_revision = "f17a6c4b9d77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factor_feature_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_release_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_release_checksum", sa.String(length=64), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("framework_version", sa.String(length=32), nullable=False),
        sa.Column("feature_names", sa.JSON(), nullable=False),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_release_id"],
            ["research_dataset_releases.release_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_feature_snapshots_snapshot_id",
        "factor_feature_snapshots",
        ["snapshot_id"],
        unique=True,
    )
    op.create_index(
        "ix_factor_feature_snapshots_dataset_release_id",
        "factor_feature_snapshots",
        ["dataset_release_id"],
    )
    op.create_index(
        "ix_factor_feature_snapshots_dataset_release_checksum",
        "factor_feature_snapshots",
        ["dataset_release_checksum"],
    )
    op.create_index(
        "ix_factor_feature_snapshots_decision_at",
        "factor_feature_snapshots",
        ["decision_at"],
    )
    op.create_index(
        "ix_factor_feature_snapshots_published_at",
        "factor_feature_snapshots",
        ["published_at"],
    )
    op.create_index(
        "ix_factor_feature_snapshots_checksum",
        "factor_feature_snapshots",
        ["checksum"],
        unique=True,
    )
    op.create_index(
        "ix_factor_feature_release_decision",
        "factor_feature_snapshots",
        ["dataset_release_id", "decision_at"],
    )

    op.create_table(
        "factor_signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("factor_name", sa.String(length=64), nullable=False),
        sa.Column("factor_version", sa.String(length=32), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("feature_snapshot_checksum", sa.String(length=64), nullable=False),
        sa.Column("candidate_universe_version", sa.String(length=128), nullable=False),
        sa.Column("research_status", sa.String(length=24), nullable=False),
        sa.Column("validation_experiment_id", sa.String(length=32), nullable=True),
        sa.Column("symbol_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["factor_feature_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_experiment_id"],
            ["research_experiments.experiment_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_signals_signal_id",
        "factor_signals",
        ["signal_id"],
        unique=True,
    )
    op.create_index(
        "ix_factor_signals_factor_name",
        "factor_signals",
        ["factor_name"],
    )
    op.create_index(
        "ix_factor_signals_feature_snapshot_id",
        "factor_signals",
        ["feature_snapshot_id"],
    )
    op.create_index(
        "ix_factor_signals_research_status",
        "factor_signals",
        ["research_status"],
    )
    op.create_index(
        "ix_factor_signals_validation_experiment_id",
        "factor_signals",
        ["validation_experiment_id"],
    )
    op.create_index(
        "ix_factor_signals_checksum",
        "factor_signals",
        ["checksum"],
        unique=True,
    )
    op.create_index(
        "ix_factor_signals_created_at",
        "factor_signals",
        ["created_at"],
    )
    op.create_index(
        "ix_factor_signal_factor_status_created",
        "factor_signals",
        ["factor_name", "research_status", "created_at"],
    )

    op.create_table(
        "factor_experiments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("factor_names", sa.JSON(), nullable=False),
        sa.Column("dataset_release_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_release_checksum", sa.String(length=64), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("comparison_group", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("validation_experiment_id", sa.String(length=32), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_release_id"],
            ["research_dataset_releases.release_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["factor_feature_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_experiment_id"],
            ["research_experiments.experiment_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_experiments_experiment_id",
        "factor_experiments",
        ["experiment_id"],
        unique=True,
    )
    op.create_index(
        "ix_factor_experiments_dataset_release_id",
        "factor_experiments",
        ["dataset_release_id"],
    )
    op.create_index(
        "ix_factor_experiments_feature_snapshot_id",
        "factor_experiments",
        ["feature_snapshot_id"],
    )
    op.create_index(
        "ix_factor_experiments_comparison_group",
        "factor_experiments",
        ["comparison_group"],
    )
    op.create_index(
        "ix_factor_experiments_status",
        "factor_experiments",
        ["status"],
    )
    op.create_index(
        "ix_factor_experiments_validation_experiment_id",
        "factor_experiments",
        ["validation_experiment_id"],
    )
    op.create_index(
        "ix_factor_experiments_created_at",
        "factor_experiments",
        ["created_at"],
    )
    op.create_index(
        "ix_factor_experiment_group_status_created",
        "factor_experiments",
        ["comparison_group", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factor_experiment_group_status_created",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_validation_experiment_id",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_created_at",
        table_name="factor_experiments",
        if_exists=True,
    )
    op.drop_index(
        "ix_factor_experiments_status",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_comparison_group",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_feature_snapshot_id",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_dataset_release_id",
        table_name="factor_experiments",
    )
    op.drop_index(
        "ix_factor_experiments_experiment_id",
        table_name="factor_experiments",
    )
    op.drop_table("factor_experiments")

    op.drop_index(
        "ix_factor_signal_factor_status_created",
        table_name="factor_signals",
    )
    op.drop_index("ix_factor_signals_checksum", table_name="factor_signals")
    op.drop_index(
        "ix_factor_signals_validation_experiment_id",
        table_name="factor_signals",
    )
    op.drop_index(
        "ix_factor_signals_created_at",
        table_name="factor_signals",
        if_exists=True,
    )
    op.drop_index(
        "ix_factor_signals_research_status",
        table_name="factor_signals",
    )
    op.drop_index(
        "ix_factor_signals_feature_snapshot_id",
        table_name="factor_signals",
    )
    op.drop_index("ix_factor_signals_factor_name", table_name="factor_signals")
    op.drop_index("ix_factor_signals_signal_id", table_name="factor_signals")
    op.drop_table("factor_signals")

    op.drop_index(
        "ix_factor_feature_release_decision",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_checksum",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_published_at",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_decision_at",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_dataset_release_checksum",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_dataset_release_id",
        table_name="factor_feature_snapshots",
    )
    op.drop_index(
        "ix_factor_feature_snapshots_snapshot_id",
        table_name="factor_feature_snapshots",
    )
    op.drop_table("factor_feature_snapshots")
