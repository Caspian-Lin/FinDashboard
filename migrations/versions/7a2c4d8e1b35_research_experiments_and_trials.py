"""research_experiments_and_trials

Revision ID: 7a2c4d8e1b35
Revises: 8c1f9b3a4d2e
Create Date: 2026-07-28 11:00:00.000000

issue #57: 新增 ``research_experiments`` 和 ``research_trials`` 两张表,
用于样本外验证流水线 —— 实验登记、假设冻结、试验计数、揭盲状态、统计修正报告。

约束:
* ``research_experiments.experiment_id`` UNIQUE;
* ``research_trials.trial_id`` UNIQUE;
* ``research_trials`` 通过 ``experiment_id`` FK 关联实验,CASCADE 删除;
* ``(experiment_id, trial_index)`` UNIQUE,防止 trial_index 冲突。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7a2c4d8e1b35"
down_revision = "8c1f9b3a4d2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_experiments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("version_stamp", sa.JSON(), nullable=False),
        sa.Column("version_checksum", sa.String(length=64), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("robustness", sa.JSON(), nullable=False),
        sa.Column(
            "strategy_params_space",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trials_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "final_test_unsealed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("supersedes_id", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", name="uq_research_experiment_experiment_id"),
    )
    op.create_index(
        "ix_research_experiments_experiment_id",
        "research_experiments",
        ["experiment_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_experiments_version_checksum",
        "research_experiments",
        ["version_checksum"],
    )
    op.create_index(
        "ix_research_experiments_status",
        "research_experiments",
        ["status"],
    )
    op.create_index(
        "ix_research_experiments_created_at",
        "research_experiments",
        ["created_at"],
    )
    op.create_index(
        "ix_research_experiments_supersedes_id",
        "research_experiments",
        ["supersedes_id"],
    )
    op.create_index(
        "ix_research_experiment_status_created",
        "research_experiments",
        ["status", "created_at"],
    )

    op.create_table(
        "research_trials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trial_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("trial_index", sa.Integer(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("in_sample_metrics", sa.JSON(), nullable=True),
        sa.Column("oos_metrics", sa.JSON(), nullable=True),
        sa.Column(
            "walk_forward_windows",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "robustness_probes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("statistical_report", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["research_experiments.experiment_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "trial_index",
            name="uq_research_trial_experiment_index",
        ),
    )
    op.create_index(
        "ix_research_trials_trial_id",
        "research_trials",
        ["trial_id"],
        unique=True,
    )
    op.create_index(
        "ix_research_trials_experiment_id",
        "research_trials",
        ["experiment_id"],
    )
    op.create_index(
        "ix_research_trials_status",
        "research_trials",
        ["status"],
    )
    op.create_index(
        "ix_research_trial_experiment_status",
        "research_trials",
        ["experiment_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_trial_experiment_status", table_name="research_trials"
    )
    op.drop_index("ix_research_trials_status", table_name="research_trials")
    op.drop_index(
        "ix_research_trials_experiment_id", table_name="research_trials"
    )
    op.drop_index(
        "ix_research_trials_trial_id", table_name="research_trials"
    )
    op.drop_table("research_trials")

    op.drop_index(
        "ix_research_experiment_status_created", table_name="research_experiments"
    )
    op.drop_index(
        "ix_research_experiments_supersedes_id", table_name="research_experiments"
    )
    op.drop_index(
        "ix_research_experiments_created_at", table_name="research_experiments"
    )
    op.drop_index(
        "ix_research_experiments_status", table_name="research_experiments"
    )
    op.drop_index(
        "ix_research_experiments_version_checksum",
        table_name="research_experiments",
    )
    op.drop_index(
        "ix_research_experiments_experiment_id",
        table_name="research_experiments",
    )
    op.drop_table("research_experiments")
