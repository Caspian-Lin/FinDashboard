"""research_code_runs_216

Revision ID: d3c4f5a6b7c8
Revises: c1d2e3f4a5b6
Create Date: 2026-08-29 12:00:00.000000

issue #216:研究代码沙箱执行记录表。一次 ``kind=research_code_run`` 后台
任务的审计与产物登记 —— code commit × 数据 release × 输出 checksum 三向
引用、镜像 digest、退出码 / 超时 / OOM / 资源用量。纯研究域存储,不触
实盘表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3c4f5a6b7c8"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_code_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=48), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("commit", sa.String(length=40), nullable=False),
        sa.Column("code_checksum", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=True),
        sa.Column(
            "dataset_release_ids",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "dataset_release_checksums",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("image", sa.String(length=128), nullable=False),
        sa.Column("image_digest", sa.String(length=160), nullable=False),
        sa.Column("mount_manifest_checksum", sa.String(length=64), nullable=True),
        sa.Column("scores_checksum", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column(
            "timed_out",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "oom_killed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("artifact_dir", sa.String(length=260), nullable=False),
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
        "ix_research_code_runs_run_id",
        "research_code_runs",
        ["run_id"],
        unique=True,
    )
    op.create_index("ix_research_code_runs_job_id", "research_code_runs", ["job_id"])
    op.create_index("ix_research_code_runs_kind", "research_code_runs", ["kind"])
    op.create_index("ix_research_code_runs_name", "research_code_runs", ["name"])
    op.create_index("ix_research_code_runs_status", "research_code_runs", ["status"])
    op.create_index(
        "ix_research_code_runs_created_at", "research_code_runs", ["created_at"]
    )
    op.create_index(
        "ix_research_code_runs_kind_name_status",
        "research_code_runs",
        ["kind", "name", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_code_runs_kind_name_status", table_name="research_code_runs"
    )
    op.drop_index("ix_research_code_runs_created_at", table_name="research_code_runs")
    op.drop_index("ix_research_code_runs_status", table_name="research_code_runs")
    op.drop_index("ix_research_code_runs_name", table_name="research_code_runs")
    op.drop_index("ix_research_code_runs_kind", table_name="research_code_runs")
    op.drop_index("ix_research_code_runs_job_id", table_name="research_code_runs")
    op.drop_index("ix_research_code_runs_run_id", table_name="research_code_runs")
    op.drop_table("research_code_runs")
