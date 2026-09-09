"""financial_indicators_batch3_fields_401

Revision ID: 765d46a5f29e
Revises: e9a0b1c2d3f4
Create Date: 2026-09-09 12:00:00.000000

issue #401:fina_indicator 白名单扩展(批次 3)—— ``research_financial_indicators``
新增 29 个可空数值列,解锁 Growth(YoY / 单季 YoY / 单季 QoQ)与 Quality
(ROA / 周转率族 / 流动速动比率 / ICR / 现金流质量)约 40 个预置因子。

全部新列 nullable 且默认 NULL:存量行零影响(旧行重读即缺测,与上游
fina_indicator 对非银行/特定行业的空值口径一致);tushare 字段映射、
领域 dataclass、发布白名单三方与表列的一致性由单测锁定。

回滚 = downgrade 逐列收回(纯加列,无数据改写;须先确认无依赖新列的
发布仍在消费,回滚后旧发布产物不受影响——发布产物不可变)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "765d46a5f29e"
down_revision = "e9a0b1c2d3f4"
branch_labels = None
depends_on = None

_TABLE = "research_financial_indicators"

#: 与 ``ResearchFinancialIndicatorModel`` / ``FINANCIAL_INDICATORS_FIELDS``
#: 新增部分逐一同名(单测锁定三方一致)。
_NEW_COLUMNS: tuple[str, ...] = (
    # 增长(YoY / 单季 YoY / 单季 QoQ)
    "operating_revenue_yoy",
    "basic_eps_yoy",
    "deducted_netprofit_yoy",
    "operating_profit_yoy",
    "revenue_yoy_q",
    "revenue_qoq",
    "netprofit_yoy_q",
    "netprofit_qoq",
    # 盈利质量(ROA / ROIC / 扣非 / 单季盈利 / 期间费用率)
    "return_on_assets",
    "return_on_assets_np",
    "roe_deducted",
    "roic",
    "roe_q",
    "return_on_assets_q",
    "grossprofit_margin_q",
    "netprofit_margin_q",
    "expense_to_revenue",
    # 营运效率(周转率族)
    "inventory_turnover",
    "receivables_turnover",
    "current_assets_turnover",
    "fixed_assets_turnover",
    "total_assets_turnover",
    # 流动性 / 偿债
    "current_ratio",
    "quick_ratio",
    "debt_to_equity",
    "interest_coverage",
    "equity_multiplier",
    # 现金流质量
    "ocf_to_revenue",
    "ocf_to_debt",
)

_NUMERIC = sa.Numeric(28, 8, decimal_return_scale=8)


def upgrade() -> None:
    for column in _NEW_COLUMNS:
        op.add_column(_TABLE, sa.Column(column, _NUMERIC, nullable=True))


def downgrade() -> None:
    for column in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, column)
