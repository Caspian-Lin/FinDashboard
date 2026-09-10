"""Tushare 研究数据 Provider。

SDK 在显式构造 Provider 且未注入 client 时才加载。外部响应会先完整规范化,
任意一行不满足契约都会拒绝整批结果,避免把部分坏数据伪装成有效快照。

例外:全市场枚举接口(``stock_basic`` / ``namechange``,#389 起 daily_basic /
cb_basic 亦然,#392 统一口径)按行解析,单行契约违规(如退市档案的历史前缀
代码 T600018.SH、namechange 的 X19363.SH)跳过并具名告警,不再炸整批同步;
批级护栏(截断防护、状态一致性、全部行被跳过)仍 fail-closed。行级口径不再
由方法隐含,而是经 ``dirty_row_policy`` 显式声明(#392,dataset_sync 框架按
SyncSpec 枚举形态分发):``None`` = 各方法历史默认;``"skip"`` = 行级跳过
(仅全市场枚举方法);``"reject"`` = 整批拒(按 symbol 精确查询恒为此)。
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import structlog

from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    ConvertibleProfile,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
    IndustryMembership,
    InstrumentNameChange,
    InstrumentProfile,
    ResearchDataConfigurationError,
    ResearchDataContractError,
    ResearchDataDependencyError,
    ResearchDataUpstreamError,
    SuspensionRecord,
)
from finboard_data.tushare_budget import TushareBudget, shared_tushare_budget

_SOURCE = "tushare"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_VALID_LIST_STATUSES = frozenset({"L", "D", "P", "G"})
_TEN_THOUSAND = Decimal("10000")
_ONE_HUNDRED = Decimal("100")

#: 行级质量口径策略(#389 固化、#392 框架分发):"skip" 行级跳过 +
#: 具名告警;"reject" 整批拒;None = 各方法历史默认。
_ROW_POLICY_SKIP = "skip"
_ROW_POLICY_REJECT = "reject"
_ROW_POLICIES = frozenset({_ROW_POLICY_SKIP, _ROW_POLICY_REJECT})

logger = structlog.get_logger(__name__)

_STOCK_BASIC_FIELDS = "ts_code,name,industry,market,exchange,list_status,list_date,delist_date"
_DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv,limit_status"
)
#: fina_indicator 字段映射表(issue #401 批次 3 扩展,单一事实源):
#: ``(tushare 列名, 领域字段名, 换算方式)``。换算方式:
#: * ``"percent"`` —— 上游百分数,``_percentage`` 归一为小数(3.2 → 0.032),
#:   同比/环比/利润率/回报率/费用率族;
#: * ``"decimal"`` —— 上游原值小数(2026-09 实测口径):周转率(次/报告期)、
#:   流动/速动比率、产权比率、ICR(倍数)、权益乘数、现金流比率
#:   (ocf_to_or ≈ 0.54 即 54%,上游给的是比率不是百分数)。
#: 发布白名单 ``FINANCIAL_INDICATORS_FIELDS`` 与表列(迁移 #401)以领域
#: 字段名为准,三方一致性由单测锁定。
_FINANCIAL_FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    # 基础每股/回报(#171 既有 12 字段)
    ("eps", "eps", "decimal"),
    ("dt_eps", "diluted_eps", "decimal"),
    ("bps", "book_value_per_share", "decimal"),
    ("ocfps", "operating_cash_flow_per_share", "decimal"),
    ("roe", "return_on_equity", "percent"),
    ("roe_waa", "weighted_return_on_equity", "percent"),
    ("grossprofit_margin", "gross_profit_margin", "percent"),
    ("netprofit_margin", "net_profit_margin", "percent"),
    ("debt_to_assets", "debt_to_assets", "percent"),
    ("tr_yoy", "revenue_yoy", "percent"),
    ("netprofit_yoy", "net_profit_yoy", "percent"),
    ("ocf_yoy", "operating_cash_flow_yoy", "percent"),
    # 增长:YoY / 单季 YoY / 单季 QoQ(#401)
    ("or_yoy", "operating_revenue_yoy", "percent"),
    ("basic_eps_yoy", "basic_eps_yoy", "percent"),
    ("dt_netprofit_yoy", "deducted_netprofit_yoy", "percent"),
    ("op_yoy", "operating_profit_yoy", "percent"),
    ("q_gr_yoy", "revenue_yoy_q", "percent"),
    ("q_gr_qoq", "revenue_qoq", "percent"),
    ("q_netprofit_yoy", "netprofit_yoy_q", "percent"),
    ("q_netprofit_qoq", "netprofit_qoq", "percent"),
    # 盈利质量:ROA / ROIC / 扣非 / 单季盈利 / 期间费用率(#401)
    ("roa", "return_on_assets", "percent"),
    ("npta", "return_on_assets_np", "percent"),
    ("roe_dt", "roe_deducted", "percent"),
    ("roic", "roic", "percent"),
    ("q_roe", "roe_q", "percent"),
    ("q_npta", "return_on_assets_q", "percent"),
    ("q_gsprofit_margin", "grossprofit_margin_q", "percent"),
    ("q_netprofit_margin", "netprofit_margin_q", "percent"),
    ("expense_of_sales", "expense_to_revenue", "percent"),
    # 营运效率:周转率族(次/报告期,原值小数,#401)
    ("inv_turn", "inventory_turnover", "decimal"),
    ("ar_turn", "receivables_turnover", "decimal"),
    ("ca_turn", "current_assets_turnover", "decimal"),
    ("fa_turn", "fixed_assets_turnover", "decimal"),
    ("assets_turn", "total_assets_turnover", "decimal"),
    # 流动性 / 偿债(倍数或比率,原值小数,#401)
    ("current_ratio", "current_ratio", "decimal"),
    ("quick_ratio", "quick_ratio", "decimal"),
    ("debt_to_eqt", "debt_to_equity", "decimal"),
    ("ebit_to_interest", "interest_coverage", "decimal"),
    ("assets_to_eqt", "equity_multiplier", "decimal"),
    # 现金流质量(比率,原值小数,#401)
    ("ocf_to_or", "ocf_to_revenue", "decimal"),
    ("ocf_to_debt", "ocf_to_debt", "decimal"),
)
_FINANCIAL_FIELDS = (
    "ts_code,ann_date,end_date,update_flag,"
    + ",".join(column for column, _, _ in _FINANCIAL_FIELD_MAP)
)
# issue #397:三表 + dividend 字段白名单(2000 积分档;上游列名原样透传,
# 领域记录属性名与上游列名一致,仅身份/时间字段改名)。契约 required 检查
# 只要求身份列(ts_code/ann_date/end_date),值列缺失走 _optional_* → None
# (财报字段稀疏是常态,见 #187 all_null_fields 语义)。
_INCOME_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,update_flag,"
    "basic_eps,diluted_eps,total_revenue,revenue,int_income,int_exp,"
    "fv_value_chg_gain,invest_income,total_cogs,oper_cost,biz_tax_surchg,"
    "sell_exp,admin_exp,fin_exp,rd_exp,assets_impair_loss,operate_profit,"
    "non_oper_income,non_oper_exp,total_profit,income_tax,n_income,"
    "n_income_attr_p,minority_gain,oth_compr_income,t_compr_income,"
    "compr_inc_attr_p,ebit,ebitda,distable_profit,continued_net_profit"
)
_BALANCE_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,update_flag,"
    "total_share,money_cap,trading_fl,notes_receiv,accounts_receiv,oth_receiv,"
    "prepayment,inventories,total_cur_assets,lt_eqt_invest,fix_assets,cip,"
    "intan_assets,goodwill,defer_tax_assets,total_nca,total_assets,st_borr,"
    "notes_payable,acct_payable,adv_receipts,contract_liab,payroll_payable,"
    "taxes_payable,non_cur_liab_due_1y,oth_cur_liab,total_cur_liab,lt_borr,"
    "bond_payable,total_ncl,total_liab,cap_rese,surplus_rese,undistr_porfit,"
    "treasury_share,minority_int,total_hldr_eqy_exc_min_int,"
    "total_hldr_eqy_inc_min_int"
)
_CASHFLOW_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,update_flag,"
    "net_profit,finan_exp,c_fr_sale_sg,recp_tax_rends,c_inf_fr_operate_a,"
    "c_paid_goods_s,c_paid_to_for_empl,c_paid_for_taxes,oth_cash_pay_oper_act,"
    "st_cash_out_act,n_cashflow_act,c_recp_return_invest,n_recp_disp_fiolta,"
    "stot_inflows_inv_act,c_pay_acq_const_fiolta,c_paid_invest,"
    "stot_out_inv_act,n_cashflow_inv_act,c_recp_borrow,proc_issue_bonds,"
    "stot_cash_in_fnc_act,c_prepay_amt_borr,c_pay_dist_dpcp_int_exp,"
    "incl_dvd_profit_paid_sc_ms,stot_cashout_fnc_act,n_cash_flows_fnc_act,"
    "eff_fx_flu_cash,n_incr_cash_cash_equ,c_cash_equ_beg_period,"
    "c_cash_equ_end_period,free_cashflow,depr_fa_coga_dpba,"
    "amort_intang_assets,credit_impa_loss,loss_fv_chg,invest_loss"
)
#: dividend 上游无 update_flag;``div_proc``(预案/股东大会通过/实施/不分配…)
#: 是同一(分红年度, 公告日)多行进展的判别符并进身份键(600519.SH 实测:
#: 同日同年度「预案」与「股东大会通过」两行并存)。列名 ``div_listdate``
#: 是上游拼写的股份到账日。
_DIVIDEND_FIELDS = (
    "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
    "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
    "imp_ann_date"
)
#: 三表按 symbol + 公告日窗查询,单窗行数 = 报告期数 x 报告口径数;即便
#: 跨十年窗口也在数百行量级,1000 行截断护栏足够宽且能挡 schema 突变。
_STATEMENT_LIMIT = 1000
#: dividend 单标的全历史(分红年度数 x 进展行数)同样远低于该值。
_DIVIDEND_LIMIT = 1000
_INDUSTRY_FIELDS = (
    "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new"
)
_NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,change_reason"
#: namechange 单次返回上限以下的安全页大小;超过一页时按 offset 循环拉全。
_NAMECHANGE_PAGE_SIZE = 5000
#: 可转债基础条款字段白名单(issue #265,doc_id=185)。cb_basic 无评级字段,
#: 评级由 akshare bond_zh_cov 兜底(dataset_sync 合并);swap_price 是
#: **当前**转股价快照(下修史不在覆盖范围)。
_CB_BASIC_FIELDS = (
    "ts_code,bond_full_name,bond_short_name,stock_code,stock_name,list_date,"
    "delist_date,swap_price,value_date,mature_date,coupon_rate"
)
#: cb_basic 在市 + 摘牌合计千级;远低于该值的安全截断护栏。
_CB_BASIC_LIMIT = 5000
#: suspend_d 单日停复牌字段白名单(issue #396,doc_id=214,2000 积分档)。
_SUSPEND_D_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
#: suspend_d 按日全市场单日行数的安全截断护栏(全市场 ~5400 只,单日停复牌
#: 枚举远低于该值)。
_SUSPEND_D_LIMIT = 6000
#: suspend_type 词表(S=停牌,R=复牌);suspend_timing 非空表示盘中停牌。
_SUSPEND_TYPE_SUSPEND = "S"
_SUSPEND_TYPE_RESUME = "R"
#: 停复牌 kind 词表(与缓存侧 TushareLifecycleEvent.event_type 同词表,
#: 便于 research_suspensions 与缓存侧 suspension_events 对账)。
SUSPEND_KIND_SUSPENSION_DAY = "suspension_day"
SUSPEND_KIND_INTRADAY = "intraday_suspension"
SUSPEND_KIND_RESUMPTION = "resumption"


def _resolve_skip_dirty_rows(policy: str | None, *, default: bool) -> bool:
    """归一行级口径:None 回落方法默认,非法值具名拒绝(fail-closed)。"""

    if policy is None:
        return default
    normalized = policy.strip().lower()
    if normalized not in _ROW_POLICIES:
        raise ResearchDataConfigurationError(
            f"dirty_row_policy 必须是 {_ROW_POLICY_SKIP} 或 {_ROW_POLICY_REJECT},收到: {policy!r}"
        )
    return normalized == _ROW_POLICY_SKIP


def _reject_skip_for_symbol_query(policy: str | None, endpoint: str) -> None:
    """按 symbol 精确查询不支持行级跳过(坏行意味着该标的数据异常)。"""

    if policy is not None and policy.strip().lower() == _ROW_POLICY_SKIP:
        raise ResearchDataConfigurationError(
            f"{endpoint} 按 symbol 精确查询不支持 dirty_row_policy=skip;"
            "单行契约违规一律整批拒"
        )


class TushareClient(Protocol):
    """Provider 所需的最小 Tushare client 形状,便于离线测试注入。"""

    def stock_basic(self, **kwargs: str) -> object:
        """调用 ``stock_basic``。"""
        ...

    def daily_basic(self, **kwargs: str) -> object:
        """调用 ``daily_basic``。"""
        ...

    def fina_indicator(self, **kwargs: str) -> object:
        """调用 ``fina_indicator``。"""
        ...

    def income(self, **kwargs: str) -> object:
        """调用 ``income``(利润表,#397)。"""
        ...

    def balancesheet(self, **kwargs: str) -> object:
        """调用 ``balancesheet``(资产负债表,#397)。"""
        ...

    def cashflow(self, **kwargs: str) -> object:
        """调用 ``cashflow``(现金流量表,#397)。"""
        ...

    def dividend(self, **kwargs: str) -> object:
        """调用 ``dividend``(分红送股,#397)。"""
        ...

    def index_member_all(self, **kwargs: str) -> object:
        """调用 ``index_member_all``。"""
        ...

    def namechange(self, **kwargs: str) -> object:
        """调用 ``namechange``(历史名称变更,#251)。"""
        ...

    def cb_basic(self, **kwargs: str) -> object:
        """调用 ``cb_basic``(可转债基础条款,#265)。"""
        ...

    def suspend_d(self, **kwargs: str) -> object:
        """调用 ``suspend_d``(每日停复牌信息,#396)。"""
        ...


class TushareResearchDataProvider:
    """读取并规范化 Tushare 研究数据。

    :param token: 显式 token;省略时读取 ``FINBOARD_TUSHARE_TOKEN``。
    :param client: 可注入的 SDK client。注入时不需要 token,适合测试和离线适配。
    :param now: 可注入时钟,必须返回 timezone-aware ``datetime``。
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        client: TushareClient | None = None,
        now: Callable[[], datetime] | None = None,
        budget: TushareBudget | None = None,
        requests_per_minute: int = 200,
        daily_request_limit: int = 100_000,
        usage_file: str = "data_cache/tushare_usage.json",
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._client = client if client is not None else self._create_client(token)
        self._budget = budget or shared_tushare_budget(
            requests_per_minute=requests_per_minute,
            daily_request_limit=daily_request_limit,
            usage_file=usage_file,
        )

    def __repr__(self) -> str:
        """返回不含凭据的稳定表示。"""
        return "TushareResearchDataProvider(source='tushare')"

    async def fetch_instrument_profiles(
        self,
        *,
        list_status: str = "L",
        dirty_row_policy: str | None = None,
    ) -> list[InstrumentProfile]:
        """读取股票档案;不把当前快照伪装成历史时点数据。

        全市场枚举:单行契约违规(退市档案的历史前缀代码等)默认跳过并具名
        告警 ``tushare.dirty_row_skipped``(``dirty_row_policy="reject"`` 可显式
        收紧为整批拒);截断防护与状态一致性检查仍整批拒绝。
        """
        skip_dirty = _resolve_skip_dirty_rows(dirty_row_policy, default=True)
        normalized_status = list_status.strip().upper()
        if normalized_status not in _VALID_LIST_STATUSES:
            raise ResearchDataConfigurationError("list_status 必须是 L、D、P 或 G")
        observed_at = self._observed_at()
        rows = await self._call(
            "stock_basic",
            exchange="",
            list_status=normalized_status,
            fields=_STOCK_BASIC_FIELDS,
        )
        _reject_possible_truncation(rows, "stock_basic", limit=6000)
        profiles = self._parse_rows(
            rows,
            self._parse_instrument,
            observed_at=observed_at,
            endpoint="stock_basic",
            skip_dirty_rows=skip_dirty,
        )
        if any(item.list_status != normalized_status for item in profiles):
            raise ResearchDataContractError("Tushare stock_basic 返回了请求状态之外的记录")
        return sorted(profiles, key=lambda item: item.symbol)

    async def fetch_daily_metrics(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[DailySecurityMetrics]:
        """读取全市场每日指标,最早可用时间固定为交易日 17:00(上海时区)。

        按日全市场枚举(#392 统一口径):默认整批拒;``dirty_row_policy="skip"``
        时单行契约违规跳过并具名告警(截断防护与交易日一致性检查仍整批拒)。
        """
        skip_dirty = _resolve_skip_dirty_rows(dirty_row_policy, default=False)
        observed_at = self._observed_at()
        rows = await self._call(
            "daily_basic",
            ts_code="",
            trade_date=_format_date(trade_date),
            fields=_DAILY_BASIC_FIELDS,
        )
        _reject_possible_truncation(rows, "daily_basic", limit=6000)
        metrics = self._parse_rows(
            rows,
            self._parse_daily_metric,
            observed_at=observed_at,
            endpoint="daily_basic",
            skip_dirty_rows=skip_dirty,
        )
        if any(item.trade_date != trade_date for item in metrics):
            raise ResearchDataContractError("Tushare daily_basic 返回了请求交易日之外的记录")
        return sorted(metrics, key=lambda item: item.symbol)

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
        dirty_row_policy: str | None = None,
    ) -> list[FinancialIndicator]:
        """读取单只股票的财务指标,保留公告修订版本。

        按 symbol 精确查询:单行契约违规整批拒;``dirty_row_policy="skip"``
        被显式拒绝(参数仅为框架统一透传而接受,#392)。
        """
        _reject_skip_for_symbol_query(dirty_row_policy, "fina_indicator")
        normalized_symbol = _normalize_symbol(symbol)
        if start_period > end_period:
            raise ResearchDataConfigurationError("start_period 不能晚于 end_period")
        kwargs = {
            "ts_code": normalized_symbol,
            "fields": _FINANCIAL_FIELDS,
            "start_date": _format_date(start_period),
            "end_date": _format_date(end_period),
        }

        observed_at = self._observed_at()
        rows = await self._call("fina_indicator", **kwargs)
        _reject_possible_truncation(rows, "fina_indicator", limit=100)
        indicators = [
            self._parse_financial(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in indicators):
            raise ResearchDataContractError("Tushare fina_indicator 返回了请求标的之外的记录")
        return sorted(
            indicators,
            key=lambda item: (
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
            ),
        )

    async def fetch_income_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[IncomeStatement]:
        """读取单只股票公告日窗内的利润表修订(issue #397,2000 积分档)。

        ``start_date``/``end_date`` 按 tushare 语义过滤**公告日期**(与
        fina_indicator 切片键同口径);保留全部修订版本与报告口径行。
        按 symbol 精确查询:单行契约违规整批拒;``dirty_row_policy="skip"``
        被显式拒绝(参数仅为框架统一透传而接受,#392)。
        """
        _reject_skip_for_symbol_query(dirty_row_policy, "income")
        normalized_symbol = _normalize_symbol(symbol)
        if start_announced > end_announced:
            raise ResearchDataConfigurationError("start_announced 不能晚于 end_announced")
        observed_at = self._observed_at()
        rows = await self._call(
            "income",
            ts_code=normalized_symbol,
            fields=_INCOME_FIELDS,
            start_date=_format_date(start_announced),
            end_date=_format_date(end_announced),
        )
        _reject_possible_truncation(rows, "income", limit=_STATEMENT_LIMIT)
        parsed = [
            _parse_announced_statement(
                row,
                index,
                observed_at,
                endpoint="income",
                fields=_INCOME_FIELDS,
                build=lambda kw: IncomeStatement(**kw),
            )
            for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in parsed):
            raise ResearchDataContractError("Tushare income 返回了请求标的之外的记录")
        return sorted(parsed, key=_statement_sort_key)

    async def fetch_balance_sheets(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[BalanceSheet]:
        """读取单只股票公告日窗内的资产负债表修订(issue #397)。"""
        _reject_skip_for_symbol_query(dirty_row_policy, "balancesheet")
        normalized_symbol = _normalize_symbol(symbol)
        if start_announced > end_announced:
            raise ResearchDataConfigurationError("start_announced 不能晚于 end_announced")
        observed_at = self._observed_at()
        rows = await self._call(
            "balancesheet",
            ts_code=normalized_symbol,
            fields=_BALANCE_FIELDS,
            start_date=_format_date(start_announced),
            end_date=_format_date(end_announced),
        )
        _reject_possible_truncation(rows, "balancesheet", limit=_STATEMENT_LIMIT)
        parsed = [
            _parse_announced_statement(
                row,
                index,
                observed_at,
                endpoint="balancesheet",
                fields=_BALANCE_FIELDS,
                build=lambda kw: BalanceSheet(**kw),
            )
            for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in parsed):
            raise ResearchDataContractError(
                "Tushare balancesheet 返回了请求标的之外的记录"
            )
        return sorted(parsed, key=_statement_sort_key)

    async def fetch_cashflow_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[CashflowStatement]:
        """读取单只股票公告日窗内的现金流量表修订(issue #397)。"""
        _reject_skip_for_symbol_query(dirty_row_policy, "cashflow")
        normalized_symbol = _normalize_symbol(symbol)
        if start_announced > end_announced:
            raise ResearchDataConfigurationError("start_announced 不能晚于 end_announced")
        observed_at = self._observed_at()
        rows = await self._call(
            "cashflow",
            ts_code=normalized_symbol,
            fields=_CASHFLOW_FIELDS,
            start_date=_format_date(start_announced),
            end_date=_format_date(end_announced),
        )
        _reject_possible_truncation(rows, "cashflow", limit=_STATEMENT_LIMIT)
        parsed = [
            _parse_announced_statement(
                row,
                index,
                observed_at,
                endpoint="cashflow",
                fields=_CASHFLOW_FIELDS,
                build=lambda kw: CashflowStatement(**kw),
            )
            for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in parsed):
            raise ResearchDataContractError("Tushare cashflow 返回了请求标的之外的记录")
        return sorted(parsed, key=_statement_sort_key)

    async def fetch_dividends(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[DividendRecord]:
        """读取单只股票公告日窗内的分红送股进展(issue #397,2000 积分档)。

        同一(分红年度, 公告日)存在预案 / 股东大会通过 / 实施多条进展行,
        ``div_proc`` 进身份键全保留(不取最新,读端按需筛选进展)。

        上游 ``dividend`` 接口**没有** start_date/end_date 区间参数(2026-09
        真实 API 实测:传了也被服务端忽略,返回该标的全历史),窗口过滤在
        客户端按 ``ann_date`` 执行 —— 切片键(dataset_version)与实际返回
        范围保持一致,跨窗口批次不重复携带窗外的行。
        """
        _reject_skip_for_symbol_query(dirty_row_policy, "dividend")
        normalized_symbol = _normalize_symbol(symbol)
        if start_announced > end_announced:
            raise ResearchDataConfigurationError("start_announced 不能晚于 end_announced")
        observed_at = self._observed_at()
        rows = await self._call(
            "dividend",
            ts_code=normalized_symbol,
            fields=_DIVIDEND_FIELDS,
        )
        _reject_possible_truncation(rows, "dividend", limit=_DIVIDEND_LIMIT)
        parsed = [
            self._parse_dividend(row, index, observed_at)
            for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in parsed):
            raise ResearchDataContractError("Tushare dividend 返回了请求标的之外的记录")
        return sorted(
            (
                item
                for item in parsed
                if start_announced <= item.announcement_date <= end_announced
            ),
            key=lambda item: (
                item.report_period,
                item.announcement_date,
                item.div_proc,
            ),
        )

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
        dirty_row_policy: str | None = None,
    ) -> list[IndustryMembership]:
        """读取申万 2021 行业成员;无历史发布时间时按本次观察时间可用。

        按 symbol 精确查询:单行契约违规整批拒;``dirty_row_policy="skip"``
        被显式拒绝(参数仅为框架统一透传而接受,#392)。
        """
        _reject_skip_for_symbol_query(dirty_row_policy, "index_member_all")
        normalized_symbol = _normalize_symbol(symbol)
        kwargs = {"fields": _INDUSTRY_FIELDS, "ts_code": normalized_symbol}
        if current_only:
            kwargs["is_new"] = "Y"

        observed_at = self._observed_at()
        rows = await self._call("index_member_all", **kwargs)
        _reject_possible_truncation(rows, "index_member_all", limit=2000)
        memberships = [
            self._parse_industry(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in memberships):
            raise ResearchDataContractError("Tushare index_member_all 返回了请求标的之外的记录")
        return sorted(
            memberships,
            key=lambda item: (item.symbol, item.effective_from, item.level3_code),
        )

    async def fetch_name_changes(
        self,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[InstrumentNameChange]:
        """读取全市场历史名称变更(分页拉全;#251 名称历史 PIT 导入)。

        ``namechange`` 每行自带 ``start_date``/``end_date`` 业务有效区间,直接
        对应 ``instrument_names`` 的半开区间,供 #213 ST-PIT 按决策日取名称。
        接口单次返回有上限,按 ``offset`` 循环直到取尽,每页各自消耗限流预算;
        名称变更支持 Pit(tushare 提供的就是历史区间),``available_at`` 取本次
        观察时间(上游无历史发布时间)。全市场枚举:单行契约违规(历史前缀
        代码等)默认跳过并具名告警 ``tushare.dirty_row_skipped``,页级护栏
        照常生效。
        """
        skip_dirty = _resolve_skip_dirty_rows(dirty_row_policy, default=True)
        observed_at = self._observed_at()
        changes: list[InstrumentNameChange] = []
        offset = 0
        while True:
            rows = await self._call(
                "namechange",
                fields=_NAMECHANGE_FIELDS,
                limit=str(_NAMECHANGE_PAGE_SIZE),
                offset=str(offset),
            )
            if not rows:
                break
            changes.extend(
                self._parse_rows(
                    rows,
                    self._parse_name_change,
                    observed_at=observed_at,
                    endpoint="namechange",
                    skip_dirty_rows=skip_dirty,
                )
            )
            if len(rows) < _NAMECHANGE_PAGE_SIZE:
                break
            offset += _NAMECHANGE_PAGE_SIZE
        return sorted(changes, key=lambda item: (item.symbol, item.start_date))

    async def fetch_convertible_profiles(
        self,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[ConvertibleProfile]:
        """读取全市场可转债基础条款(issue #265,2000 积分档)。

        在市(L)+ 摘牌(D)档案分两次拉取合并进同一批(与 #251 股票档案
        同风格,摘牌档案携带 delist_date);同 symbol 去重保留在市记录。
        cb_basic 是当前快照,``available_at`` = 本次观察时间;限流经
        ``tushare_budget`` 与其他接口共享配额。全市场枚举(#392 统一口径):
        默认整批拒;``dirty_row_policy="skip"`` 时单行契约违规跳过并具名告警
        (截断防护与同 symbol 去重语义不变)。
        """
        skip_dirty = _resolve_skip_dirty_rows(dirty_row_policy, default=False)
        observed_at = self._observed_at()
        by_symbol: dict[str, ConvertibleProfile] = {}
        # 先 D 后 L:L(在市)记录对同 symbol 权威(真实数据两态互斥,这里
        # 只防御上游同 symbol 双写);D 记录填补摘牌债的 delist_date。
        for list_status in ("D", "L"):
            rows = await self._call(
                "cb_basic",
                list_status=list_status,
                fields=_CB_BASIC_FIELDS,
            )
            _reject_possible_truncation(rows, "cb_basic", limit=_CB_BASIC_LIMIT)
            parsed = self._parse_rows(
                rows,
                self._parse_convertible_profile,
                observed_at=observed_at,
                endpoint="cb_basic",
                skip_dirty_rows=skip_dirty,
            )
            for item in parsed:
                if list_status == "L" or item.symbol not in by_symbol:
                    by_symbol[item.symbol] = item
        return sorted(by_symbol.values(), key=lambda item: item.symbol)

    async def fetch_suspensions(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[SuspensionRecord]:
        """读取指定交易日的全市场停复牌枚举(issue #396,2000 积分档)。

        按日全市场枚举(#392 统一口径):默认整批拒;``dirty_row_policy="skip"``
        时单行契约违规跳过并具名告警(截断防护与交易日一致性检查仍整批拒)。
        非交易日上游返回空列表(框架按空切片跳过,不产生批次行)。
        ``suspend_kind`` 与缓存侧 ``TushareLifecycleEvent.event_type`` 同词表;
        PIT=当日:``available_at`` = 交易日 09:30(上海,开盘即可观察)。
        """
        skip_dirty = _resolve_skip_dirty_rows(dirty_row_policy, default=False)
        observed_at = self._observed_at()
        rows = await self._call(
            "suspend_d",
            trade_date=_format_date(trade_date),
            fields=_SUSPEND_D_FIELDS,
        )
        _reject_possible_truncation(rows, "suspend_d", limit=_SUSPEND_D_LIMIT)
        records = self._parse_rows(
            rows,
            self._parse_suspension,
            observed_at=observed_at,
            endpoint="suspend_d",
            skip_dirty_rows=skip_dirty,
        )
        if any(item.trade_date != trade_date for item in records):
            raise ResearchDataContractError(
                "Tushare suspend_d 返回了请求交易日之外的记录"
            )
        return sorted(records, key=lambda item: (item.symbol, item.suspend_kind))

    @staticmethod
    def _parse_suspension(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> SuspensionRecord:
        endpoint = "suspend_d"
        _require_fields(row, _SUSPEND_D_FIELDS, endpoint, index)
        business_date = _required_date(row, "trade_date", endpoint, index)
        suspend_type = _required_text(row, "suspend_type", endpoint, index).upper()
        suspend_timing = _optional_text(row, "suspend_timing")
        if suspend_type == _SUSPEND_TYPE_SUSPEND:
            kind = (
                SUSPEND_KIND_INTRADAY
                if suspend_timing
                else SUSPEND_KIND_SUSPENSION_DAY
            )
        elif suspend_type == _SUSPEND_TYPE_RESUME:
            kind = SUSPEND_KIND_RESUMPTION
        else:
            raise _field_error(
                endpoint, index, "suspend_type", "必须是 S(停牌)或 R(复牌)"
            )
        return SuspensionRecord(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            trade_date=business_date,
            suspend_kind=kind,
            suspend_type=suspend_type,
            suspend_timing=suspend_timing,
            source=_SOURCE,
            observed_at=observed_at,
            available_at=datetime.combine(
                business_date,
                time(hour=9, minute=30),
                tzinfo=_SHANGHAI,
            ),
        )

    @staticmethod
    def _parse_dividend(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> DividendRecord:
        endpoint = "dividend"
        _require_fields(row, _DIVIDEND_FIELDS, endpoint, index)
        announcement_date = _required_date(row, "ann_date", endpoint, index)
        return DividendRecord(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            announcement_date=announcement_date,
            report_period=_required_date(row, "end_date", endpoint, index),
            div_proc=_required_text(row, "div_proc", endpoint, index),
            stk_div=_optional_decimal(row, "stk_div", endpoint, index),
            stk_bo_rate=_optional_decimal(row, "stk_bo_rate", endpoint, index),
            stk_co_rate=_optional_decimal(row, "stk_co_rate", endpoint, index),
            cash_div=_optional_decimal(row, "cash_div", endpoint, index),
            cash_div_tax=_optional_decimal(row, "cash_div_tax", endpoint, index),
            record_date=_optional_date(row, "record_date", endpoint, index),
            ex_date=_optional_date(row, "ex_date", endpoint, index),
            pay_date=_optional_date(row, "pay_date", endpoint, index),
            div_listdate=_optional_date(row, "div_listdate", endpoint, index),
            imp_ann_date=_optional_date(row, "imp_ann_date", endpoint, index),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=_announced_available_at(announcement_date),
        )

    def _create_client(self, explicit_token: str | None) -> TushareClient:
        token = (
            explicit_token if explicit_token is not None else os.getenv("FINBOARD_TUSHARE_TOKEN")
        )
        if token is None or not token.strip():
            raise ResearchDataConfigurationError(
                "未配置 Tushare token;请设置 FINBOARD_TUSHARE_TOKEN"
            )
        try:
            module = importlib.import_module("tushare")
        except ImportError:
            raise ResearchDataDependencyError(
                "未安装 Tushare SDK;请安装 finboard-data[tushare]"
            ) from None

        factory_object = getattr(module, "pro_api", None)
        if not callable(factory_object):
            raise ResearchDataDependencyError("已安装的 Tushare SDK 不提供 pro_api")
        factory = cast(Callable[[str], object], factory_object)
        try:
            client = factory(token.strip())
        except Exception:
            raise ResearchDataUpstreamError("Tushare client 初始化失败") from None
        return cast(TushareClient, client)

    def _observed_at(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResearchDataConfigurationError("Tushare Provider 时钟必须返回带时区的 datetime")
        return value

    def _parse_rows[T](
        self,
        rows: Sequence[Mapping[str, object]],
        parse: Callable[[Mapping[str, object], int, datetime], T],
        *,
        observed_at: datetime,
        endpoint: str,
        skip_dirty_rows: bool,
    ) -> list[T]:
        """按行级口径解析(#389 固化、#392 框架按 SyncSpec 形态分发)。

        ``skip_dirty_rows=True``(全市场枚举):单行契约违规跳过并具名告警
        ``tushare.dirty_row_skipped`` —— 全市场档案覆盖含历史前缀代码的退市
        老股(T600018.SH 等),单条脏行不值得炸整批同步;但「全部行被跳过」
        意味着上游 schema 破坏而非孤立脏数据,仍按整批拒绝处理。
        ``skip_dirty_rows=False``(按 symbol 精确查询 / 默认整批拒):单行
        违规直接抛出。
        """
        parsed: list[T] = []
        for index, row in enumerate(rows):
            try:
                parsed.append(parse(row, index, observed_at))
            except ResearchDataContractError as exc:
                if not skip_dirty_rows:
                    raise
                logger.warning(
                    "tushare.dirty_row_skipped",
                    endpoint=endpoint,
                    index=index,
                    ts_code=row.get("ts_code"),
                    name=row.get("name"),
                    reason=str(exc),
                )
        if skip_dirty_rows and rows and not parsed:
            raise ResearchDataContractError(
                f"Tushare {endpoint} 全部 {len(rows)} 行均违反契约,疑似上游 schema 变更"
            )
        return parsed

    async def _call(self, endpoint: str, **kwargs: str) -> list[Mapping[str, object]]:
        method_object = getattr(self._client, endpoint, None)
        if not callable(method_object):
            raise ResearchDataDependencyError(f"Tushare client 不支持 {endpoint}")
        method = cast(Callable[..., object], method_object)
        await self._budget.acquire()
        try:
            payload = await asyncio.to_thread(method, **kwargs)
        except Exception:
            raise ResearchDataUpstreamError(f"Tushare {endpoint} 调用失败") from None
        return _records(payload, endpoint)

    @staticmethod
    def _parse_instrument(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> InstrumentProfile:
        endpoint = "stock_basic"
        _require_fields(row, _STOCK_BASIC_FIELDS, endpoint, index)
        return InstrumentProfile(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            name=_required_text(row, "name", endpoint, index),
            exchange=_required_text(row, "exchange", endpoint, index),
            market=_required_text(row, "market", endpoint, index),
            list_status=_required_text(row, "list_status", endpoint, index),
            list_date=_required_date(row, "list_date", endpoint, index),
            delist_date=_optional_date(row, "delist_date", endpoint, index),
            industry=_optional_text(row, "industry"),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )

    @staticmethod
    def _parse_daily_metric(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> DailySecurityMetrics:
        endpoint = "daily_basic"
        _require_fields(row, _DAILY_BASIC_FIELDS, endpoint, index)
        business_date = _required_date(row, "trade_date", endpoint, index)
        return DailySecurityMetrics(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            trade_date=business_date,
            close=_optional_decimal(row, "close", endpoint, index),
            turnover_rate=_percentage(row, "turnover_rate", endpoint, index),
            turnover_rate_free=_percentage(row, "turnover_rate_f", endpoint, index),
            volume_ratio=_optional_decimal(row, "volume_ratio", endpoint, index),
            pe=_optional_decimal(row, "pe", endpoint, index),
            pe_ttm=_optional_decimal(row, "pe_ttm", endpoint, index),
            pb=_optional_decimal(row, "pb", endpoint, index),
            ps=_optional_decimal(row, "ps", endpoint, index),
            ps_ttm=_optional_decimal(row, "ps_ttm", endpoint, index),
            dividend_yield=_percentage(row, "dv_ratio", endpoint, index),
            dividend_yield_ttm=_percentage(row, "dv_ttm", endpoint, index),
            total_shares=_scaled_decimal(row, "total_share", _TEN_THOUSAND, endpoint, index),
            float_shares=_scaled_decimal(row, "float_share", _TEN_THOUSAND, endpoint, index),
            free_shares=_scaled_decimal(row, "free_share", _TEN_THOUSAND, endpoint, index),
            total_market_cap=_scaled_decimal(row, "total_mv", _TEN_THOUSAND, endpoint, index),
            circulating_market_cap=_scaled_decimal(row, "circ_mv", _TEN_THOUSAND, endpoint, index),
            limit_status=_optional_integer(row, "limit_status", endpoint, index),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=datetime.combine(
                business_date,
                time(hour=17),
                tzinfo=_SHANGHAI,
            ),
        )

    @staticmethod
    def _parse_financial(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> FinancialIndicator:
        endpoint = "fina_indicator"
        _require_fields(row, _FINANCIAL_FIELDS, endpoint, index)
        announcement_date = _required_date(row, "ann_date", endpoint, index)
        # 字段换算随 _FINANCIAL_FIELD_MAP 单一事实源驱动(percent → 小数,
        # decimal 原值);映射表与领域字段/白名单/表列的一致性由单测锁定。
        values = {
            name: (
                _percentage(row, column, endpoint, index)
                if kind == "percent"
                else _optional_decimal(row, column, endpoint, index)
            )
            for column, name, kind in _FINANCIAL_FIELD_MAP
        }
        return FinancialIndicator(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            announcement_date=announcement_date,
            report_period=_required_date(row, "end_date", endpoint, index),
            update_flag=_optional_text(row, "update_flag"),
            **values,
            source=_SOURCE,
            observed_at=observed_at,
            available_at=datetime.combine(
                announcement_date + timedelta(days=1),
                time.min,
                tzinfo=_SHANGHAI,
            ),
        )

    @staticmethod
    def _parse_industry(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> IndustryMembership:
        endpoint = "index_member_all"
        _require_fields(row, _INDUSTRY_FIELDS, endpoint, index)
        current_flag = _required_text(row, "is_new", endpoint, index).upper()
        if current_flag not in {"Y", "N"}:
            raise _field_error(endpoint, index, "is_new", "必须是 Y 或 N")
        return IndustryMembership(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            security_name=_required_text(row, "name", endpoint, index),
            taxonomy="SW2021",
            level1_code=_required_text(row, "l1_code", endpoint, index),
            level1_name=_required_text(row, "l1_name", endpoint, index),
            level2_code=_required_text(row, "l2_code", endpoint, index),
            level2_name=_required_text(row, "l2_name", endpoint, index),
            level3_code=_required_text(row, "l3_code", endpoint, index),
            level3_name=_required_text(row, "l3_name", endpoint, index),
            effective_from=_required_date(row, "in_date", endpoint, index),
            effective_to=_optional_date(row, "out_date", endpoint, index),
            is_current=current_flag == "Y",
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )

    @staticmethod
    def _parse_name_change(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> InstrumentNameChange:
        endpoint = "namechange"
        _require_fields(row, _NAMECHANGE_FIELDS, endpoint, index)
        return InstrumentNameChange(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            name=_required_text(row, "name", endpoint, index),
            start_date=_required_date(row, "start_date", endpoint, index),
            end_date=_optional_date(row, "end_date", endpoint, index),
            change_reason=_optional_text(row, "change_reason"),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )

    @staticmethod
    def _parse_convertible_profile(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> ConvertibleProfile:
        endpoint = "cb_basic"
        _require_fields(row, _CB_BASIC_FIELDS, endpoint, index)
        swap_price = _optional_decimal(row, "swap_price", endpoint, index)
        # 转债转股价必须为正;0/负值视同缺失(缺失在下游质量报告可见)。
        if swap_price is not None and swap_price <= 0:
            swap_price = None
        return ConvertibleProfile(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            name=_required_text(row, "bond_short_name", endpoint, index),
            underlying_symbol=_normalize_symbol(
                _required_text(row, "stock_code", endpoint, index)
            ),
            underlying_name=_optional_text(row, "stock_name"),
            list_date=_optional_date(row, "list_date", endpoint, index),
            delist_date=_optional_date(row, "delist_date", endpoint, index),
            conversion_price=swap_price,
            issue_date=_optional_date(row, "value_date", endpoint, index),
            maturity_date=_optional_date(row, "mature_date", endpoint, index),
            coupon_rate=_percentage(row, "coupon_rate", endpoint, index),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )


def _announced_available_at(announcement_date: date) -> datetime:
    """公告类研究数据的 PIT 可见时间:ann_date+1 零点(上海,#212 口径)。"""

    return datetime.combine(
        announcement_date + timedelta(days=1),
        time.min,
        tzinfo=_SHANGHAI,
    )


def _statement_sort_key(item: object) -> tuple[date, date, str, str, str]:
    """三表修订行的稳定排序(与 fina_indicator 同风格,多两列报告口径)。"""
    return (
        item.report_period,  # type: ignore[attr-defined]
        item.announcement_date,  # type: ignore[attr-defined]
        item.update_flag or "",  # type: ignore[attr-defined]
        item.report_type or "",  # type: ignore[attr-defined]
        item.comp_type or "",  # type: ignore[attr-defined]
    )


def _parse_announced_statement[T](
    row: Mapping[str, object],
    index: int,
    observed_at: datetime,
    *,
    endpoint: str,
    fields: str,
    build: Callable[[dict[str, Any]], T],
) -> T:
    """三表共享解析:身份列必填,值列全部 optional(财报稀疏是常态,#187)。

    ``build`` 把扁平关键字字典构造成具体数据类;字段名与白名单一致,值列
    统一 ``_optional_decimal``(金额单位人民币元,无单位换算)。
    """
    _require_fields(row, fields, endpoint, index)
    announcement_date = _required_date(row, "ann_date", endpoint, index)
    kwargs: dict[str, Any] = {
        "symbol": _normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
        "announcement_date": announcement_date,
        "report_period": _required_date(row, "end_date", endpoint, index),
        "formal_announcement_date": _optional_date(
            row, "f_ann_date", endpoint, index
        ),
        "report_type": _optional_text(row, "report_type"),
        "comp_type": _optional_text(row, "comp_type"),
        "update_flag": _optional_text(row, "update_flag"),
        "source": _SOURCE,
        "observed_at": observed_at,
        "available_at": _announced_available_at(announcement_date),
    }
    for name in _field_names(fields):
        if name in {"ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "update_flag"}:
            continue
        kwargs[name] = _optional_decimal(row, name, endpoint, index)
    return build(kwargs)


def _records(payload: object, endpoint: str) -> list[Mapping[str, object]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        raw_records: object = payload
    else:
        converter_object = getattr(payload, "to_dict", None)
        if not callable(converter_object):
            raise ResearchDataContractError(f"Tushare {endpoint} 返回值不是记录列表或 DataFrame")
        converter = cast(Callable[..., object], converter_object)
        try:
            raw_records = converter(orient="records")
        except Exception:
            raise ResearchDataContractError(f"Tushare {endpoint} 响应无法转换为记录") from None

    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes, bytearray)):
        raise ResearchDataContractError(f"Tushare {endpoint} 响应记录格式无效")
    result: list[Mapping[str, object]] = []
    for index, row in enumerate(raw_records):
        if not isinstance(row, Mapping):
            raise ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行不是字段映射")
        if not all(isinstance(key, str) for key in row):
            raise ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行包含非字符串字段名")
        result.append(cast(Mapping[str, object], row))
    return result


def _reject_possible_truncation(
    rows: Sequence[Mapping[str, object]],
    endpoint: str,
    *,
    limit: int,
) -> None:
    if len(rows) >= limit:
        raise ResearchDataContractError(
            f"Tushare {endpoint} 返回 {len(rows)} 行并达到接口上限 {limit};"
            "结果可能被截断,请缩小查询范围"
        )


def _field_names(fields: str) -> frozenset[str]:
    return frozenset(fields.split(","))


def _require_fields(
    row: Mapping[str, object],
    fields: str,
    endpoint: str,
    index: int,
) -> None:
    missing = sorted(_field_names(fields) - row.keys())
    if missing:
        raise ResearchDataContractError(
            f"Tushare {endpoint} 第 {index} 行缺少字段: {', '.join(missing)}"
        )


def _normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ResearchDataContractError("股票代码必须是 6 位数字并使用 .SH、.SZ 或 .BJ 后缀")
    return normalized


def _format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "nat", "<na>"}
    if isinstance(value, float):
        return math.isnan(value)
    return isinstance(value, Decimal) and value.is_nan()


def _optional_text(row: Mapping[str, object], field: str) -> str | None:
    value = row[field]
    if _is_missing(value):
        return None
    return str(value).strip()


def _required_text(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> str:
    value = _optional_text(row, field)
    if value is None:
        raise _field_error(endpoint, index, field, "不能为空")
    return value


def _optional_date(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> date | None:
    value = row[field]
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        raise _field_error(endpoint, index, field, "不是有效的 YYYYMMDD 日期") from None


def _required_date(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> date:
    value = _optional_date(row, field, endpoint, index)
    if value is None:
        raise _field_error(endpoint, index, field, "不能为空")
    return value


def _optional_decimal(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = row[field]
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        raise _field_error(endpoint, index, field, "不是有效数值")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _field_error(endpoint, index, field, "不是有效数值") from None
    if decimal_value.is_nan():
        return None
    if not decimal_value.is_finite():
        raise _field_error(endpoint, index, field, "必须是有限数值")
    return decimal_value


def _scaled_decimal(
    row: Mapping[str, object],
    field: str,
    multiplier: Decimal,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = _optional_decimal(row, field, endpoint, index)
    return None if value is None else value * multiplier


def _percentage(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = _optional_decimal(row, field, endpoint, index)
    return None if value is None else value / _ONE_HUNDRED


def _optional_integer(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> int | None:
    value = _optional_decimal(row, field, endpoint, index)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise _field_error(endpoint, index, field, "必须是整数")
    return int(value)


def _field_error(
    endpoint: str,
    index: int,
    field: str,
    message: str,
) -> ResearchDataContractError:
    return ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行字段 {field} {message}")
