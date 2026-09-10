"""research_income_statements / balance_sheets / cashflow_statements / dividends_397

Revision ID: c159fcd63f94
Revises: e5f6a7b8c9d0
Create Date: 2026-09-09 12:00:00.000000

issue #397:财务面扩展 —— 三表 + dividend 分红明细研究数据集。

四个新表(批次发布语义,挂 ``research_sync_batches.id``,可由同步任务重建):

* ``research_income_statements`` —— tushare ``income`` 利润表(QMJ 盈利性/
  成长性支柱、EBITDA、每股收益);
* ``research_balance_sheets`` —— tushare ``balancesheet`` 资产负债表(安全性
  支柱、流动/速动比率与存货/应收应付周转原料);
* ``research_cashflow_statements`` —— tushare ``cashflow`` 现金流量表
  (FCF/OCF/capex、盈利质量交叉验证);
* ``research_dividends`` —— tushare ``dividend`` 分红送股进展(除权除息日/
  现金股利/送转明细,精确股息率因子原料)。

修订可见性:三表身份 = (symbol, report_period, announcement_date, update_flag,
report_type, comp_type),同一报告期多版本/多口径行并存不互相覆盖
(fina_indicator 先例);dividend 上游无 update_flag,以 ``div_proc``
(预案/股东大会通过/实施...)进身份键全保留。PIT=ann_date+1 零点(上海,
#212 口径),``available_at`` 列承载,发布与读取端按列门控。

回滚 = downgrade 删四表(可由同步任务重建,无不可再生数据)。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c159fcd63f94"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_income_statements",
            sa.Column("batch_id", sa.BigInteger(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("dataset_version", sa.String(length=128), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("announcement_date", sa.Date(), nullable=False),
            sa.Column("report_period", sa.Date(), nullable=False),
            sa.Column("formal_announcement_date", sa.Date(), nullable=True),
            sa.Column("report_type", sa.String(length=16), nullable=False),
            sa.Column("comp_type", sa.String(length=16), nullable=False),
            sa.Column("update_flag", sa.String(length=16), nullable=False),
            sa.Column("basic_eps", sa.Numeric(28, 8), nullable=True),
            sa.Column("diluted_eps", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_revenue", sa.Numeric(28, 8), nullable=True),
            sa.Column("revenue", sa.Numeric(28, 8), nullable=True),
            sa.Column("int_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("int_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("fv_value_chg_gain", sa.Numeric(28, 8), nullable=True),
            sa.Column("invest_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_cogs", sa.Numeric(28, 8), nullable=True),
            sa.Column("oper_cost", sa.Numeric(28, 8), nullable=True),
            sa.Column("biz_tax_surchg", sa.Numeric(28, 8), nullable=True),
            sa.Column("sell_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("admin_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("fin_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("rd_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("assets_impair_loss", sa.Numeric(28, 8), nullable=True),
            sa.Column("operate_profit", sa.Numeric(28, 8), nullable=True),
            sa.Column("non_oper_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("non_oper_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_profit", sa.Numeric(28, 8), nullable=True),
            sa.Column("income_tax", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_income_attr_p", sa.Numeric(28, 8), nullable=True),
            sa.Column("minority_gain", sa.Numeric(28, 8), nullable=True),
            sa.Column("oth_compr_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("t_compr_income", sa.Numeric(28, 8), nullable=True),
            sa.Column("compr_inc_attr_p", sa.Numeric(28, 8), nullable=True),
            sa.Column("ebit", sa.Numeric(28, 8), nullable=True),
            sa.Column("ebitda", sa.Numeric(28, 8), nullable=True),
            sa.Column("distable_profit", sa.Numeric(28, 8), nullable=True),
            sa.Column("continued_net_profit", sa.Numeric(28, 8), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "ingested_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["research_sync_batches.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "dataset_version",
                "symbol",
                "report_period",
                "announcement_date",
                "update_flag",
                "report_type",
                "comp_type",
                name="uq_research_income_revision",
            ),
        )
    op.create_index(
        "ix_research_income_symbol_period", "research_income_statements", ["symbol", "report_period"]
    )
    op.create_index(
        "ix_research_income_period_symbol", "research_income_statements", ["report_period", "symbol"]
    )
    op.create_index(
        op.f("ix_research_income_statements_available_at"),
        "research_income_statements",
        ["available_at"],
    )
    op.create_table(
        "research_balance_sheets",
            sa.Column("batch_id", sa.BigInteger(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("dataset_version", sa.String(length=128), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("announcement_date", sa.Date(), nullable=False),
            sa.Column("report_period", sa.Date(), nullable=False),
            sa.Column("formal_announcement_date", sa.Date(), nullable=True),
            sa.Column("report_type", sa.String(length=16), nullable=False),
            sa.Column("comp_type", sa.String(length=16), nullable=False),
            sa.Column("update_flag", sa.String(length=16), nullable=False),
            sa.Column("total_share", sa.Numeric(28, 8), nullable=True),
            sa.Column("money_cap", sa.Numeric(28, 8), nullable=True),
            sa.Column("trading_fl", sa.Numeric(28, 8), nullable=True),
            sa.Column("notes_receiv", sa.Numeric(28, 8), nullable=True),
            sa.Column("accounts_receiv", sa.Numeric(28, 8), nullable=True),
            sa.Column("oth_receiv", sa.Numeric(28, 8), nullable=True),
            sa.Column("prepayment", sa.Numeric(28, 8), nullable=True),
            sa.Column("inventories", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_cur_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("lt_eqt_invest", sa.Numeric(28, 8), nullable=True),
            sa.Column("fix_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("cip", sa.Numeric(28, 8), nullable=True),
            sa.Column("intan_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("goodwill", sa.Numeric(28, 8), nullable=True),
            sa.Column("defer_tax_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_nca", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("st_borr", sa.Numeric(28, 8), nullable=True),
            sa.Column("notes_payable", sa.Numeric(28, 8), nullable=True),
            sa.Column("acct_payable", sa.Numeric(28, 8), nullable=True),
            sa.Column("adv_receipts", sa.Numeric(28, 8), nullable=True),
            sa.Column("contract_liab", sa.Numeric(28, 8), nullable=True),
            sa.Column("payroll_payable", sa.Numeric(28, 8), nullable=True),
            sa.Column("taxes_payable", sa.Numeric(28, 8), nullable=True),
            sa.Column("non_cur_liab_due_1y", sa.Numeric(28, 8), nullable=True),
            sa.Column("oth_cur_liab", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_cur_liab", sa.Numeric(28, 8), nullable=True),
            sa.Column("lt_borr", sa.Numeric(28, 8), nullable=True),
            sa.Column("bond_payable", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_ncl", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_liab", sa.Numeric(28, 8), nullable=True),
            sa.Column("cap_rese", sa.Numeric(28, 8), nullable=True),
            sa.Column("surplus_rese", sa.Numeric(28, 8), nullable=True),
            sa.Column("undistr_porfit", sa.Numeric(28, 8), nullable=True),
            sa.Column("treasury_share", sa.Numeric(28, 8), nullable=True),
            sa.Column("minority_int", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_hldr_eqy_exc_min_int", sa.Numeric(28, 8), nullable=True),
            sa.Column("total_hldr_eqy_inc_min_int", sa.Numeric(28, 8), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "ingested_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["research_sync_batches.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "dataset_version",
                "symbol",
                "report_period",
                "announcement_date",
                "update_flag",
                "report_type",
                "comp_type",
                name="uq_research_balance_revision",
            ),
        )
    op.create_index(
        "ix_research_balance_symbol_period", "research_balance_sheets", ["symbol", "report_period"]
    )
    op.create_index(
        "ix_research_balance_period_symbol", "research_balance_sheets", ["report_period", "symbol"]
    )
    op.create_index(
        op.f("ix_research_balance_sheets_available_at"),
        "research_balance_sheets",
        ["available_at"],
    )
    op.create_table(
        "research_cashflow_statements",
            sa.Column("batch_id", sa.BigInteger(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("dataset_version", sa.String(length=128), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("announcement_date", sa.Date(), nullable=False),
            sa.Column("report_period", sa.Date(), nullable=False),
            sa.Column("formal_announcement_date", sa.Date(), nullable=True),
            sa.Column("report_type", sa.String(length=16), nullable=False),
            sa.Column("comp_type", sa.String(length=16), nullable=False),
            sa.Column("update_flag", sa.String(length=16), nullable=False),
            sa.Column("net_profit", sa.Numeric(28, 8), nullable=True),
            sa.Column("finan_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_fr_sale_sg", sa.Numeric(28, 8), nullable=True),
            sa.Column("recp_tax_rends", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_inf_fr_operate_a", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_paid_goods_s", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_paid_to_for_empl", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_paid_for_taxes", sa.Numeric(28, 8), nullable=True),
            sa.Column("oth_cash_pay_oper_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("st_cash_out_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_cashflow_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_recp_return_invest", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_recp_disp_fiolta", sa.Numeric(28, 8), nullable=True),
            sa.Column("stot_inflows_inv_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_pay_acq_const_fiolta", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_paid_invest", sa.Numeric(28, 8), nullable=True),
            sa.Column("stot_out_inv_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_cashflow_inv_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_recp_borrow", sa.Numeric(28, 8), nullable=True),
            sa.Column("proc_issue_bonds", sa.Numeric(28, 8), nullable=True),
            sa.Column("stot_cash_in_fnc_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_prepay_amt_borr", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_pay_dist_dpcp_int_exp", sa.Numeric(28, 8), nullable=True),
            sa.Column("incl_dvd_profit_paid_sc_ms", sa.Numeric(28, 8), nullable=True),
            sa.Column("stot_cashout_fnc_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_cash_flows_fnc_act", sa.Numeric(28, 8), nullable=True),
            sa.Column("eff_fx_flu_cash", sa.Numeric(28, 8), nullable=True),
            sa.Column("n_incr_cash_cash_equ", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_cash_equ_beg_period", sa.Numeric(28, 8), nullable=True),
            sa.Column("c_cash_equ_end_period", sa.Numeric(28, 8), nullable=True),
            sa.Column("free_cashflow", sa.Numeric(28, 8), nullable=True),
            sa.Column("depr_fa_coga_dpba", sa.Numeric(28, 8), nullable=True),
            sa.Column("amort_intang_assets", sa.Numeric(28, 8), nullable=True),
            sa.Column("credit_impa_loss", sa.Numeric(28, 8), nullable=True),
            sa.Column("loss_fv_chg", sa.Numeric(28, 8), nullable=True),
            sa.Column("invest_loss", sa.Numeric(28, 8), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "ingested_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["research_sync_batches.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "dataset_version",
                "symbol",
                "report_period",
                "announcement_date",
                "update_flag",
                "report_type",
                "comp_type",
                name="uq_research_cashflow_revision",
            ),
        )
    op.create_index(
        "ix_research_cashflow_symbol_period", "research_cashflow_statements", ["symbol", "report_period"]
    )
    op.create_index(
        "ix_research_cashflow_period_symbol", "research_cashflow_statements", ["report_period", "symbol"]
    )
    op.create_index(
        op.f("ix_research_cashflow_statements_available_at"),
        "research_cashflow_statements",
        ["available_at"],
    )
    op.create_table(
        "research_dividends",
            sa.Column("batch_id", sa.BigInteger(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("dataset_version", sa.String(length=128), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("announcement_date", sa.Date(), nullable=False),
            sa.Column("report_period", sa.Date(), nullable=False),
            sa.Column("div_proc", sa.String(length=32), nullable=False),
            sa.Column("stk_div", sa.Numeric(28, 8), nullable=True),
            sa.Column("stk_bo_rate", sa.Numeric(28, 8), nullable=True),
            sa.Column("stk_co_rate", sa.Numeric(28, 8), nullable=True),
            sa.Column("cash_div", sa.Numeric(28, 8), nullable=True),
            sa.Column("cash_div_tax", sa.Numeric(28, 8), nullable=True),
            sa.Column("record_date", sa.Date(), nullable=True),
            sa.Column("ex_date", sa.Date(), nullable=True),
            sa.Column("pay_date", sa.Date(), nullable=True),
            sa.Column("div_listdate", sa.Date(), nullable=True),
            sa.Column("imp_ann_date", sa.Date(), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "ingested_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["research_sync_batches.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "dataset_version",
                "symbol",
                "report_period",
                "announcement_date",
                "div_proc",
                name="uq_research_dividend_revision",
            ),
        )
    op.create_index(
        "ix_research_dividend_symbol_period",
        "research_dividends",
        ["symbol", "report_period"],
    )
    op.create_index(
        "ix_research_dividend_ex_date", "research_dividends", ["ex_date"]
    )
    op.create_index(
        op.f("ix_research_dividends_available_at"),
        "research_dividends",
        ["available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_research_dividends_available_at"), table_name="research_dividends"
    )
    op.drop_index("ix_research_dividend_ex_date", table_name="research_dividends")
    op.drop_index(
        "ix_research_dividend_symbol_period", table_name="research_dividends"
    )
    op.drop_table("research_dividends")
    op.drop_index(
        op.f("ix_research_cashflow_statements_available_at"), table_name="research_cashflow_statements"
    )
    op.drop_index("ix_research_cashflow_period_symbol", table_name="research_cashflow_statements")
    op.drop_index("ix_research_cashflow_symbol_period", table_name="research_cashflow_statements")
    op.drop_table("research_cashflow_statements")
    op.drop_index(
        op.f("ix_research_balance_sheets_available_at"), table_name="research_balance_sheets"
    )
    op.drop_index("ix_research_balance_period_symbol", table_name="research_balance_sheets")
    op.drop_index("ix_research_balance_symbol_period", table_name="research_balance_sheets")
    op.drop_table("research_balance_sheets")
    op.drop_index(
        op.f("ix_research_income_statements_available_at"), table_name="research_income_statements"
    )
    op.drop_index("ix_research_income_period_symbol", table_name="research_income_statements")
    op.drop_index("ix_research_income_symbol_period", table_name="research_income_statements")
    op.drop_table("research_income_statements")
