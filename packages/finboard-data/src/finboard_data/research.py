"""时点化研究数据领域契约。

这里的模型只描述外部研究数据,不承担持久化、因子计算或交易职责。
``available_at`` 表示一条记录最早可以被研究/回测使用的时间,用于防止未来函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _dc_field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """股票档案的只读快照。

    ``available_at`` 对应本次观察时间;Tushare 的 ``stock_basic`` 不提供历史发布
    时间,因此不能仅凭 ``list_date`` 把今天看到的档案回填成历史已知数据。
    """

    symbol: str
    name: str
    exchange: str
    market: str
    list_status: str
    list_date: date
    delist_date: date | None
    industry: str | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class DailySecurityMetrics:
    """指定交易日的估值、流动性、股本和市值指标。

    比率字段均为小数(例如 2.5% 表示为 ``Decimal("0.025")``);股本单位为股,
    市值单位为人民币元。
    """

    symbol: str
    trade_date: date
    close: Decimal | None
    turnover_rate: Decimal | None
    turnover_rate_free: Decimal | None
    volume_ratio: Decimal | None
    pe: Decimal | None
    pe_ttm: Decimal | None
    pb: Decimal | None
    ps: Decimal | None
    ps_ttm: Decimal | None
    dividend_yield: Decimal | None
    dividend_yield_ttm: Decimal | None
    total_shares: Decimal | None
    float_shares: Decimal | None
    free_shares: Decimal | None
    total_market_cap: Decimal | None
    circulating_market_cap: Decimal | None
    limit_status: int | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FinancialIndicator:
    """一版已公告的财务指标。

    同一 ``report_period`` 可以存在多版公告。Provider 保留
    ``announcement_date`` 和 ``update_flag``,不在边界层覆盖修订记录。
    比率和增长率字段统一为小数。
    """

    symbol: str
    announcement_date: date
    report_period: date
    update_flag: str | None
    eps: Decimal | None
    diluted_eps: Decimal | None
    book_value_per_share: Decimal | None
    operating_cash_flow_per_share: Decimal | None
    return_on_equity: Decimal | None
    weighted_return_on_equity: Decimal | None
    gross_profit_margin: Decimal | None
    net_profit_margin: Decimal | None
    debt_to_assets: Decimal | None
    revenue_yoy: Decimal | None
    net_profit_yoy: Decimal | None
    operating_cash_flow_yoy: Decimal | None
    # ---- issue #401 批次 3 扩展字段(新字段带默认 None,旧构造零破坏)----
    # 增长(YoY / 单季 YoY / 单季 QoQ)
    operating_revenue_yoy: Decimal | None = _dc_field(default=None, kw_only=True)
    basic_eps_yoy: Decimal | None = _dc_field(default=None, kw_only=True)
    deducted_netprofit_yoy: Decimal | None = _dc_field(default=None, kw_only=True)
    operating_profit_yoy: Decimal | None = _dc_field(default=None, kw_only=True)
    revenue_yoy_q: Decimal | None = _dc_field(default=None, kw_only=True)
    revenue_qoq: Decimal | None = _dc_field(default=None, kw_only=True)
    netprofit_yoy_q: Decimal | None = _dc_field(default=None, kw_only=True)
    netprofit_qoq: Decimal | None = _dc_field(default=None, kw_only=True)
    # 盈利质量(ROA / ROIC / 扣非 / 单季盈利 / 期间费用率)
    return_on_assets: Decimal | None = _dc_field(default=None, kw_only=True)
    return_on_assets_np: Decimal | None = _dc_field(default=None, kw_only=True)
    roe_deducted: Decimal | None = _dc_field(default=None, kw_only=True)
    roic: Decimal | None = _dc_field(default=None, kw_only=True)
    roe_q: Decimal | None = _dc_field(default=None, kw_only=True)
    return_on_assets_q: Decimal | None = _dc_field(default=None, kw_only=True)
    grossprofit_margin_q: Decimal | None = _dc_field(default=None, kw_only=True)
    netprofit_margin_q: Decimal | None = _dc_field(default=None, kw_only=True)
    expense_to_revenue: Decimal | None = _dc_field(default=None, kw_only=True)
    # 营运效率(周转率族,上游为「次/报告期」倍数,原值小数)
    inventory_turnover: Decimal | None = _dc_field(default=None, kw_only=True)
    receivables_turnover: Decimal | None = _dc_field(default=None, kw_only=True)
    current_assets_turnover: Decimal | None = _dc_field(default=None, kw_only=True)
    fixed_assets_turnover: Decimal | None = _dc_field(default=None, kw_only=True)
    total_assets_turnover: Decimal | None = _dc_field(default=None, kw_only=True)
    # 流动性 / 偿债(上游为倍数或比率,原值小数)
    current_ratio: Decimal | None = _dc_field(default=None, kw_only=True)
    quick_ratio: Decimal | None = _dc_field(default=None, kw_only=True)
    debt_to_equity: Decimal | None = _dc_field(default=None, kw_only=True)
    interest_coverage: Decimal | None = _dc_field(default=None, kw_only=True)
    equity_multiplier: Decimal | None = _dc_field(default=None, kw_only=True)
    # 现金流质量(上游为比率,原值小数)
    ocf_to_revenue: Decimal | None = _dc_field(default=None, kw_only=True)
    ocf_to_debt: Decimal | None = _dc_field(default=None, kw_only=True)
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class _AnnouncedStatement:
    """三表(利润表/资产负债表/现金流量表)共用的身份与 PIT 语义基类(issue #397)。

    PIT=ann_date(#212 已核实 ann_date+1 无前视):``available_at`` = 公告日
    次日零点(上海)。同一报告期的修订版本以 ``(announcement_date,
    update_flag, report_type, comp_type)`` 区分,不互相覆盖(fina_indicator
    先例);``report_type``/``comp_type`` 进身份是三表特有的——income 等
    接口对同一报告期可能返回合并/单季/母公司等多口径行,缺了会撞修订键。
    金额单位均为人民币元(上游原样,不做缩放)。
    """

    symbol: str
    announcement_date: date
    report_period: date
    formal_announcement_date: date | None
    report_type: str | None
    comp_type: str | None
    update_flag: str | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class IncomeStatement(_AnnouncedStatement):
    """一版已公告的利润表(tushare ``income``,issue #397,2000 积分档)。

    覆盖 QMJ 盈利性/成长性支柱与 EBITDA 原料:收入/成本/三费/研发、营业利润
    到净利润全链条、ebit/ebitda(上游直接给出)、每股收益与综合收益。
    金额单位人民币元;每股字段单位元/股。
    """

    basic_eps: Decimal | None
    diluted_eps: Decimal | None
    total_revenue: Decimal | None
    revenue: Decimal | None
    int_income: Decimal | None
    int_exp: Decimal | None
    fv_value_chg_gain: Decimal | None
    invest_income: Decimal | None
    total_cogs: Decimal | None
    oper_cost: Decimal | None
    biz_tax_surchg: Decimal | None
    sell_exp: Decimal | None
    admin_exp: Decimal | None
    fin_exp: Decimal | None
    rd_exp: Decimal | None
    assets_impair_loss: Decimal | None
    operate_profit: Decimal | None
    non_oper_income: Decimal | None
    non_oper_exp: Decimal | None
    total_profit: Decimal | None
    income_tax: Decimal | None
    n_income: Decimal | None
    n_income_attr_p: Decimal | None
    minority_gain: Decimal | None
    oth_compr_income: Decimal | None
    t_compr_income: Decimal | None
    compr_inc_attr_p: Decimal | None
    ebit: Decimal | None
    ebitda: Decimal | None
    distable_profit: Decimal | None
    continued_net_profit: Decimal | None


@dataclass(frozen=True, slots=True)
class BalanceSheet(_AnnouncedStatement):
    """一版已公告的资产负债表(tushare ``balancesheet``,issue #397)。

    覆盖 QMJ 安全性支柱与营运资本/周转原料:货币资金/存货/应收应付/流动与
    非流动合计、有息负债(短借/长借/应付债券)、归母与含少数股东权益。
    流动比率 = total_cur_assets / total_cur_liab、速动比率剔除 inventories,
    均可由白名单字段直接派生。金额单位人民币元。
    """

    total_share: Decimal | None
    money_cap: Decimal | None
    trading_fl: Decimal | None
    notes_receiv: Decimal | None
    accounts_receiv: Decimal | None
    oth_receiv: Decimal | None
    prepayment: Decimal | None
    inventories: Decimal | None
    total_cur_assets: Decimal | None
    lt_eqt_invest: Decimal | None
    fix_assets: Decimal | None
    cip: Decimal | None
    intan_assets: Decimal | None
    goodwill: Decimal | None
    defer_tax_assets: Decimal | None
    total_nca: Decimal | None
    total_assets: Decimal | None
    st_borr: Decimal | None
    notes_payable: Decimal | None
    acct_payable: Decimal | None
    adv_receipts: Decimal | None
    contract_liab: Decimal | None
    payroll_payable: Decimal | None
    taxes_payable: Decimal | None
    non_cur_liab_due_1y: Decimal | None
    oth_cur_liab: Decimal | None
    total_cur_liab: Decimal | None
    lt_borr: Decimal | None
    bond_payable: Decimal | None
    total_ncl: Decimal | None
    total_liab: Decimal | None
    cap_rese: Decimal | None
    surplus_rese: Decimal | None
    undistr_porfit: Decimal | None
    treasury_share: Decimal | None
    minority_int: Decimal | None
    total_hldr_eqy_exc_min_int: Decimal | None
    total_hldr_eqy_inc_min_int: Decimal | None


@dataclass(frozen=True, slots=True)
class CashflowStatement(_AnnouncedStatement):
    """一版已公告的现金流量表(tushare ``cashflow``,issue #397)。

    覆盖 FCF/OCF 原料与盈利质量交叉验证:经营/投资/筹资三大净额、购建固定
    资产支出(capex)、上游直接给出的 free_cashflow、销售收现(c_fr_sale_sg
    对收入的质量验证)、折旧摊销(EBITDA 交叉验证)。金额单位人民币元。
    """

    net_profit: Decimal | None
    finan_exp: Decimal | None
    c_fr_sale_sg: Decimal | None
    recp_tax_rends: Decimal | None
    c_inf_fr_operate_a: Decimal | None
    c_paid_goods_s: Decimal | None
    c_paid_to_for_empl: Decimal | None
    c_paid_for_taxes: Decimal | None
    oth_cash_pay_oper_act: Decimal | None
    st_cash_out_act: Decimal | None
    n_cashflow_act: Decimal | None
    c_recp_return_invest: Decimal | None
    n_recp_disp_fiolta: Decimal | None
    stot_inflows_inv_act: Decimal | None
    c_pay_acq_const_fiolta: Decimal | None
    c_paid_invest: Decimal | None
    stot_out_inv_act: Decimal | None
    n_cashflow_inv_act: Decimal | None
    c_recp_borrow: Decimal | None
    proc_issue_bonds: Decimal | None
    stot_cash_in_fnc_act: Decimal | None
    c_prepay_amt_borr: Decimal | None
    c_pay_dist_dpcp_int_exp: Decimal | None
    incl_dvd_profit_paid_sc_ms: Decimal | None
    stot_cashout_fnc_act: Decimal | None
    n_cash_flows_fnc_act: Decimal | None
    eff_fx_flu_cash: Decimal | None
    n_incr_cash_cash_equ: Decimal | None
    c_cash_equ_beg_period: Decimal | None
    c_cash_equ_end_period: Decimal | None
    free_cashflow: Decimal | None
    depr_fa_coga_dpba: Decimal | None
    amort_intang_assets: Decimal | None
    credit_impa_loss: Decimal | None
    loss_fv_chg: Decimal | None
    invest_loss: Decimal | None


@dataclass(frozen=True, slots=True)
class DividendRecord:
    """一条分红送股进展记录(tushare ``dividend``,issue #397)。

    与三表不同:上游**没有 update_flag**,同一(报告期=分红年度,公告日)可
    以有预案 / 股东大会通过 / 实施多条进展行,``div_proc`` 是进展口径判别符
    并进身份键(600519.SH 20230630 实测:同日同年度「预案」与「股东大会
    通过」两行并存)。PIT=ann_date 同三表:``available_at`` = 公告日次日
    零点(上海)—— 预案公告即可见,除权除息日/派息日等未来业务日期随预案
    公告进入可视域,无前视。现金股利单位元/股(cash_div 税前、cash_div_tax
    税后);stk_div/stk_bo_rate/stk_co_rate 为每股口径的送转/送股/转增数量。
    精确股息率因子原料(#397);过渡期 dividend_yield_ttm 滚动近似保留。
    """

    symbol: str
    announcement_date: date
    report_period: date
    div_proc: str
    stk_div: Decimal | None
    stk_bo_rate: Decimal | None
    stk_co_rate: Decimal | None
    cash_div: Decimal | None
    cash_div_tax: Decimal | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    div_listdate: date | None
    imp_ann_date: date | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class IndustryMembership:
    """申万 2021 行业分类成员关系。

    ``effective_from``/``effective_to`` 是业务有效区间;由于上游未提供历史发布
    时间,``available_at`` 使用本次观察时间,避免把今天看到的分类误当作历史已知。
    """

    symbol: str
    security_name: str
    taxonomy: str
    level1_code: str
    level1_name: str
    level2_code: str
    level2_name: str
    level3_code: str
    level3_name: str
    effective_from: date
    effective_to: date | None
    is_current: bool
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class InstrumentNameChange:
    """一条历史名称变更记录(#251,来源 tushare ``namechange``)。

    ``start_date``/``end_date`` 是上游给的业务有效区间(半开区间语义,
    ``end_date=None`` 表示当前名称);直接对应 ``instrument_names`` 表的
    ``valid_from``/``valid_to``,供 #213 ST-PIT 按决策日取名称。
    """

    symbol: str
    name: str
    start_date: date
    end_date: date | None
    change_reason: str | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class ConvertibleProfile:
    """可转债基础条款快照(tushare ``cb_basic``,issue #265)。

    PIT 语义(诚实边界):``cb_basic`` 是**当前时点**的条款快照,不含
    转股价历史变动(下修史 / 除权除息调整史);``available_at`` = 本次
    观察时间。转股价变动历史不在覆盖范围,下游派生观测(如转股溢价率)
    不得宣称全历史 PIT。

    字段映射:``conversion_price`` ← ``swap_price``(当前转股价,可空);
    ``issue_date`` ← ``value_date``(起息日,转债语境下近似发行日);
    ``maturity_date`` ← ``mature_date``。评级不在 cb_basic 字段内,
    由 akshare ``bond_zh_cov`` 债券评级列兜底(dataset_sync 合并)。
    """

    symbol: str
    name: str
    underlying_symbol: str
    underlying_name: str | None
    list_date: date | None
    delist_date: date | None
    conversion_price: Decimal | None
    issue_date: date | None
    maturity_date: date | None
    coupon_rate: Decimal | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class SuspensionRecord:
    """单标的单日停复牌记录(tushare ``suspend_d``,issue #396)。

    ``suspend_kind`` 与缓存侧 ``TushareLifecycleEvent.event_type`` 同词表:
    ``suspension_day``(全天停牌)/ ``intraday_suspension``(盘中停牌)/
    ``resumption``(复牌)。PIT=当日:``available_at`` = 交易日 09:30
    (上海)—— 全天停牌开盘即可观察,计划停复牌按生效日可见(不早于
    生效日看到,保守方向)。
    """

    symbol: str
    trade_date: date
    suspend_kind: str
    suspend_type: str
    suspend_timing: str | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class IndexProfile:
    """指数基础信息快照(tushare ``index_basic``,issue #394)。

    PIT 语义(诚实边界):与 ``cb_basic`` 同为**当前时点**快照,不含指数
    更名 / 编码迁移史;``available_at`` = 本次观察时间。``symbol`` 保留上游
    原始代码形制(SSE/SZSE/BSE 之外还有 CSI/CIC/MSCI 等编外市场,代码段
    不止 ``6 位数字.沪深北`` 形制),登记域(哪些进 ``instruments`` 表)由
    discovery 层按 ``is_index_code`` 裁决,本记录不做 narrowing。

    ``base_date`` 是指数基日(发布机构选定的基准计算起点)——A 股三所
    指数在 ``instruments.list_date`` 上的结构化上游(issue #394:回填后
    mixed 发布的 ``missing_list_date`` 不再被指数恒 null 抬高)。上游另有
    ``list_date`` 列但大量为 null,故回填优先取 ``base_date``。
    """

    symbol: str
    name: str
    full_name: str | None
    publisher: str | None
    category: str | None
    market: str | None
    base_date: date | None
    list_date: date | None
    list_status: str | None
    source: str
    observed_at: datetime
    available_at: datetime
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FuturesContractProfile:
    """期货合约基础信息快照(tushare ``fut_basic``,issue #395)。

    PIT 语义(诚实边界):与 ``index_basic`` / ``cb_basic`` 同为**当前时点**
    快照,不含合约参数变更史(交易所调整保证金率 / 乘数公告不回溯);
    ``available_at`` = 本次观察时间。``symbol`` 是**本仓归一形制**
    (``IF2601.CFFEX``):上游 ts_code 后缀是交易所简写(``IF2601.CFX``),
    provider 归一时已映射到本仓 :data:`~finboard_data.akshare_provider.FUTURES_EXCHANGES`
    后缀,与 ``make_symbol`` / 冻结发布逐标的校验同一口径。

    ``multiplier`` / ``price_tick``:上游文档标注 multiplier 只对国债 /
    指数期货适用;``price_tick`` 从 ``quote_unit_desc``(如 ``0.2指数点``)
    解析最小变动价位,解析失败保持 None 可见缺失。**上游无保证金率列**
    —— 保证金率仍由受控登记表 / ``FuturesRule`` 承载(#267 口径),不虚构。
    """

    symbol: str
    name: str
    product: str
    exchange: str
    multiplier: Decimal | None
    price_tick: Decimal | None
    quote_unit_desc: str | None
    list_date: date | None
    delist_date: date | None
    source: str
    observed_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FuturesTradeCalendarDay:
    """期货交易日历单日观测(tushare ``fut_trade_cal``,issue #395)。

    与 #396 股票 ``trade_cal`` 表同构(exchange 区分):``is_open`` 保留
    0/1 双值(休市行同样落库,与 akshare 回源只产 ``is_open=true`` 行的
    股票口径不同 —— tushare 上游自带完整日历);``pretrade_date`` 是上一
    个交易日,供推导消费。
    """

    exchange: str
    cal_date: date
    is_open: bool
    pretrade_date: date | None
    source: str
    observed_at: datetime
    available_at: datetime


@runtime_checkable
class ResearchDataProvider(Protocol):
    """研究数据读取边界;公共接口不暴露 DataFrame 或数据源 SDK 类型。

    ``dirty_row_policy``(#392,dataset_sync 框架按 SyncSpec 形态分发):
    * ``None`` —— 各方法历史默认(全市场档案枚举跳脏行,其余整批拒);
    * ``"skip"`` —— 单行契约违规跳过 + 具名告警 ``tushare.dirty_row_skipped``
      (全市场枚举形态;按 symbol 精确查询的方法拒绝该策略);
    * ``"reject"`` —— 单行契约违规整批拒(按 symbol 精确查询恒为此)。
    """

    async def fetch_instrument_profiles(
        self,
        *,
        list_status: str = "L",
        dirty_row_policy: str | None = None,
    ) -> list[InstrumentProfile]:
        """读取指定上市状态的股票档案。"""
        ...

    async def fetch_name_changes(
        self,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[InstrumentNameChange]:
        """读取全市场历史名称变更(分页拉全;#251 名称历史 PIT 导入)。"""
        ...

    async def fetch_daily_metrics(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[DailySecurityMetrics]:
        """读取指定交易日的全市场每日指标。"""
        ...

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
        dirty_row_policy: str | None = None,
    ) -> list[FinancialIndicator]:
        """读取单只股票、指定报告期范围内的财务指标。"""
        ...

    async def fetch_income_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[IncomeStatement]:
        """读取单只股票、公告日窗内已公告的利润表修订(issue #397)。"""
        ...

    async def fetch_balance_sheets(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[BalanceSheet]:
        """读取单只股票、公告日窗内已公告的资产负债表修订(issue #397)。"""
        ...

    async def fetch_cashflow_statements(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[CashflowStatement]:
        """读取单只股票、公告日窗内已公告的现金流量表修订(issue #397)。"""
        ...

    async def fetch_dividends(
        self,
        symbol: str,
        *,
        start_announced: date,
        end_announced: date,
        dirty_row_policy: str | None = None,
    ) -> list[DividendRecord]:
        """读取单只股票、公告日窗内的分红送股进展明细(issue #397)。"""
        ...

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
        dirty_row_policy: str | None = None,
    ) -> list[IndustryMembership]:
        """读取申万行业成员关系。"""
        ...

    async def fetch_convertible_profiles(
        self,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[ConvertibleProfile]:
        """读取全市场可转债基础条款快照(在市 + 摘牌,issue #265)。"""
        ...

    async def fetch_suspensions(
        self,
        trade_date: date,
        *,
        dirty_row_policy: str | None = None,
    ) -> list[SuspensionRecord]:
        """读取指定交易日的全市场停复牌枚举(issue #396)。"""
        ...


class ResearchDataError(RuntimeError):
    """研究数据访问或规范化失败的基类。"""


class ResearchDataConfigurationError(ResearchDataError):
    """研究数据源配置缺失或无效。"""


class ResearchDataDependencyError(ResearchDataError):
    """可选数据源 SDK 未安装或不兼容。"""


class ResearchDataUpstreamError(ResearchDataError):
    """上游数据源请求失败。"""


class ResearchDataContractError(ResearchDataError):
    """上游响应不满足领域契约,整批数据应被拒绝。"""
