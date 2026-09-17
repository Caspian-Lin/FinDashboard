"""sandbox_factor_snapshot_217

Revision ID: e7d8f9a0b1c2
Revises: d3c4f5a6b7c8
Create Date: 2026-08-30 12:00:00.000000

issue #217:沙箱因子输出落库为 feature snapshot 兼容观测。

* ``factor_feature_snapshots`` 放宽 ``dataset_release_id`` 为可空并新增
  ``source_run_id`` —— 沙箱快照没有单一数据发布锚点,改锚定产出它的
  ``research_code_runs.run_id``(不建外键,run 记录自身不可变);
* ``research_code_runs`` 新增 ``output_snapshot_id`` 回填引用,形成
  run x snapshot 双向可追溯。

纯研究域存储,不触实盘表。回滚 = downgrade 删列 + 恢复非空约束。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7d8f9a0b1c2"
down_revision = "d3c4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "factor_feature_snapshots",
        "dataset_release_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.add_column(
        "factor_feature_snapshots",
        sa.Column("source_run_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_factor_feature_snapshots_source_run_id",
        "factor_feature_snapshots",
        ["source_run_id"],
    )
    op.add_column(
        "research_code_runs",
        sa.Column("output_snapshot_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_research_code_runs_output_snapshot_id",
        "research_code_runs",
        ["output_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_code_runs_output_snapshot_id",
        table_name="research_code_runs",
    )
    op.drop_column("research_code_runs", "output_snapshot_id")
    op.drop_index(
        "ix_factor_feature_snapshots_source_run_id",
        table_name="factor_feature_snapshots",
    )
    op.drop_column("factor_feature_snapshots", "source_run_id")
    op.alter_column(
        "factor_feature_snapshots",
        "dataset_release_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
