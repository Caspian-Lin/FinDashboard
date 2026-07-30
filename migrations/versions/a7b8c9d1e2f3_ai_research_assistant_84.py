"""ai_research_assistant_84

Revision ID: a7b8c9d1e2f3
Revises: f6a1b2c3d483
Create Date: 2026-07-30 20:00:00.000000

issue #84:AI 研究助手持久化 —— 因子假设、实验登记、AI 草案、审计事件。
所有表只服务研究与教育, 不写入实盘 orders/fills/positions/audit_logs。
失败 / 被拒绝记录同样保留。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d1e2f3"
down_revision = "f6a1b2c3d483"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- factor_hypotheses ---
    op.create_table(
        "factor_hypotheses",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hypothesis_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("economic_mechanism", sa.Text(), nullable=False),
        sa.Column("input_fields", sa.JSON(), nullable=False),
        sa.Column("decision_timing", sa.String(length=16), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("applicable_assets", sa.JSON(), nullable=False),
        sa.Column("expected_failure_scenarios", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column("supersedes_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("experiment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_hypotheses_hypothesis_id",
        "factor_hypotheses",
        ["hypothesis_id"],
        unique=True,
    )
    op.create_index("ix_factor_hypotheses_status", "factor_hypotheses", ["status"])
    op.create_index(
        "ix_factor_hypotheses_created_at", "factor_hypotheses", ["created_at"]
    )
    op.create_index(
        "ix_factor_hypotheses_supersedes_id",
        "factor_hypotheses",
        ["supersedes_id"],
    )
    op.create_index(
        "ix_factor_hypothesis_status_created",
        "factor_hypotheses",
        ["status", "created_at"],
    )

    # --- factor_hypothesis_experiments ---
    op.create_table(
        "factor_hypothesis_experiments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_by", sa.String(length=128), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("validation_experiment_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"],
            ["factor_hypotheses.hypothesis_id"],
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
        "ix_factor_hypothesis_experiments_experiment_id",
        "factor_hypothesis_experiments",
        ["experiment_id"],
        unique=True,
    )
    op.create_index(
        "ix_factor_hypothesis_experiments_hypothesis_id",
        "factor_hypothesis_experiments",
        ["hypothesis_id"],
    )
    op.create_index(
        "ix_factor_hypothesis_experiments_status",
        "factor_hypothesis_experiments",
        ["status"],
    )
    op.create_index(
        "ix_factor_hypothesis_experiments_validation_experiment_id",
        "factor_hypothesis_experiments",
        ["validation_experiment_id"],
    )
    op.create_index(
        "ix_fh_experiment_hypothesis_status",
        "factor_hypothesis_experiments",
        ["hypothesis_id", "status"],
    )

    # --- ai_drafts ---
    op.create_table(
        "ai_drafts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("draft_id", sa.String(length=48), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("uncertainty", sa.String(length=16), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("consumed_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_drafts_draft_id",
        "ai_drafts",
        ["draft_id"],
        unique=True,
    )
    op.create_index("ix_ai_drafts_kind", "ai_drafts", ["kind"])
    op.create_index("ix_ai_drafts_status", "ai_drafts", ["status"])
    op.create_index("ix_ai_drafts_created_at", "ai_drafts", ["created_at"])
    op.create_index(
        "ix_ai_draft_kind_status_created",
        "ai_drafts",
        ["kind", "status", "created_at"],
    )

    # --- ai_audit_events ---
    op.create_table(
        "ai_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=32), nullable=True),
        sa.Column("draft_id", sa.String(length=48), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_audit_events_event_type", "ai_audit_events", ["event_type"]
    )
    op.create_index(
        "ix_ai_audit_events_hypothesis_id", "ai_audit_events", ["hypothesis_id"]
    )
    op.create_index("ix_ai_audit_events_draft_id", "ai_audit_events", ["draft_id"])
    op.create_index(
        "ix_ai_audit_events_timestamp", "ai_audit_events", ["timestamp"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_audit_events_timestamp", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_draft_id", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_hypothesis_id", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_event_type", table_name="ai_audit_events")
    op.drop_table("ai_audit_events")

    op.drop_index("ix_ai_draft_kind_status_created", table_name="ai_drafts")
    op.drop_index("ix_ai_drafts_created_at", table_name="ai_drafts")
    op.drop_index("ix_ai_drafts_status", table_name="ai_drafts")
    op.drop_index("ix_ai_drafts_kind", table_name="ai_drafts")
    op.drop_index("ix_ai_drafts_draft_id", table_name="ai_drafts")
    op.drop_table("ai_drafts")

    op.drop_index("ix_fh_experiment_hypothesis_status", table_name="factor_hypothesis_experiments")
    op.drop_index(
        "ix_factor_hypothesis_experiments_validation_experiment_id",
        table_name="factor_hypothesis_experiments",
    )
    op.drop_index(
        "ix_factor_hypothesis_experiments_status",
        table_name="factor_hypothesis_experiments",
    )
    op.drop_index(
        "ix_factor_hypothesis_experiments_hypothesis_id",
        table_name="factor_hypothesis_experiments",
    )
    op.drop_index(
        "ix_factor_hypothesis_experiments_experiment_id",
        table_name="factor_hypothesis_experiments",
    )
    op.drop_table("factor_hypothesis_experiments")

    op.drop_index("ix_factor_hypothesis_status_created", table_name="factor_hypotheses")
    op.drop_index("ix_factor_hypotheses_supersedes_id", table_name="factor_hypotheses")
    op.drop_index("ix_factor_hypotheses_created_at", table_name="factor_hypotheses")
    op.drop_index("ix_factor_hypotheses_status", table_name="factor_hypotheses")
    op.drop_index("ix_factor_hypotheses_hypothesis_id", table_name="factor_hypotheses")
    op.drop_table("factor_hypotheses")
