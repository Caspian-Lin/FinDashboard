"""research_runs_80

Revision ID: d41e7b9c2a80
Revises: c29d8e3f0a79
Create Date: 2026-07-29 21:00:00.000000

issue #80:独立的离线 ResearchRun 与追加式血缘。禁止引用实盘账户、订单、
成交和持仓表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d41e7b9c2a80"
down_revision = "c29d8e3f0a79"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("replay_of_run_id", sa.String(length=96), nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("manifest_checksum", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("result_checksum", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_research_runs_run_id", "research_runs", ["run_id"])
    op.create_index("ix_research_runs_strategy_id", "research_runs", ["strategy_id"])
    op.create_index(
        "ix_research_runs_strategy_kind", "research_runs", ["strategy_kind"]
    )
    op.create_index("ix_research_runs_status", "research_runs", ["status"])
    op.create_index(
        "ix_research_runs_manifest_checksum",
        "research_runs",
        ["manifest_checksum"],
    )
    op.create_index("ix_research_runs_created_at", "research_runs", ["created_at"])

    op.create_table(
        "research_run_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=96), nullable=False),
        sa.Column("artifact_id", sa.String(length=160), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=48), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column(
            "parent_trace_ids",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["research_runs.run_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "artifact_id", name="uq_research_run_artifact_id"
        ),
        sa.UniqueConstraint(
            "run_id", "sequence", name="uq_research_run_artifact_sequence"
        ),
        sa.UniqueConstraint(
            "run_id", "trace_id", name="uq_research_run_trace_id"
        ),
    )
    op.create_index(
        "ix_research_run_artifacts_run_id",
        "research_run_artifacts",
        ["run_id"],
    )
    op.create_index(
        "ix_research_run_artifacts_decision_id",
        "research_run_artifacts",
        ["decision_id"],
    )
    op.create_index(
        "ix_research_run_artifacts_stage",
        "research_run_artifacts",
        ["stage"],
    )
    op.create_index(
        "ix_research_run_artifacts_trace_id",
        "research_run_artifacts",
        ["trace_id"],
    )
    op.create_index(
        "ix_research_run_artifact_history",
        "research_run_artifacts",
        ["run_id", "decision_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_run_artifact_history",
        table_name="research_run_artifacts",
    )
    op.drop_index(
        "ix_research_run_artifacts_trace_id",
        table_name="research_run_artifacts",
    )
    op.drop_index(
        "ix_research_run_artifacts_stage",
        table_name="research_run_artifacts",
    )
    op.drop_index(
        "ix_research_run_artifacts_decision_id",
        table_name="research_run_artifacts",
    )
    op.drop_index(
        "ix_research_run_artifacts_run_id",
        table_name="research_run_artifacts",
    )
    op.drop_table("research_run_artifacts")
    op.drop_index("ix_research_runs_created_at", table_name="research_runs")
    op.drop_index(
        "ix_research_runs_manifest_checksum", table_name="research_runs"
    )
    op.drop_index("ix_research_runs_status", table_name="research_runs")
    op.drop_index("ix_research_runs_strategy_kind", table_name="research_runs")
    op.drop_index("ix_research_runs_strategy_id", table_name="research_runs")
    op.drop_index("ix_research_runs_run_id", table_name="research_runs")
    op.drop_table("research_runs")
