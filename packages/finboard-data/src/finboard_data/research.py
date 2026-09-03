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
    由 akshare ``bond_zh_cov`` 债券评级列兜底(research_data_sync 合并)。
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


@runtime_checkable
class ResearchDataProvider(Protocol):
    """研究数据读取边界;公共接口不暴露 DataFrame 或数据源 SDK 类型。"""

    async def fetch_instrument_profiles(
        self,
        *,
        list_status: str = "L",
    ) -> list[InstrumentProfile]:
        """读取指定上市状态的股票档案。"""
        ...

    async def fetch_name_changes(
        self,
    ) -> list[InstrumentNameChange]:
        """读取全市场历史名称变更(分页拉全;#251 名称历史 PIT 导入)。"""
        ...

    async def fetch_daily_metrics(
        self,
        trade_date: date,
    ) -> list[DailySecurityMetrics]:
        """读取指定交易日的全市场每日指标。"""
        ...

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
    ) -> list[FinancialIndicator]:
        """读取单只股票、指定报告期范围内的财务指标。"""
        ...

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
    ) -> list[IndustryMembership]:
        """读取申万行业成员关系。"""
        ...

    async def fetch_convertible_profiles(
        self,
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
