"""时点化研究数据领域契约。

这里的模型只描述外部研究数据,不承担持久化、因子计算或交易职责。
``available_at`` 表示一条记录最早可以被研究/回测使用的时间,用于防止未来函数。
"""

from __future__ import annotations

from dataclasses import dataclass
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
