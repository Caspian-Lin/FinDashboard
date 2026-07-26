"""版本化因子目录与时点快照契约。

本模块只定义研究/回测边界,不访问数据库、行情源或交易账户。具体读取由
``FactorResearchReader`` 实现,快照持久化由 ``FactorSnapshotWriter`` 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
)

FACTOR_VERSION = "v1"


class FactorName(StrEnum):
    """首批可配置因子。"""

    MARKET_CAP = "market_cap"
    PB = "pb"
    TURNOVER_RATE = "turnover_rate"
    MOMENTUM = "momentum"
    ROE = "roe"
    GROSS_PROFIT_MARGIN = "gross_profit_margin"
    REVENUE_YOY = "revenue_yoy"


class FactorFrequency(StrEnum):
    """因子更新频率。"""

    DAILY = "daily"
    REPORT = "report"


class FactorUnit(StrEnum):
    """规范化因子单位。"""

    CNY = "cny"
    MULTIPLE = "multiple"
    RATIO = "ratio"


class PointInTimeSafety(StrEnum):
    """因子对时点数据的安全要求。"""

    STRICT = "strict"


class RankingScope(StrEnum):
    """横截面排名范围。"""

    GLOBAL = "global"
    INDUSTRY = "industry"


class FactorSnapshotStatus(StrEnum):
    """快照是否可供策略使用。"""

    PUBLISHED = "published"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    """一项因子的稳定目录声明。"""

    name: FactorName
    version: str
    frequency: FactorFrequency
    unit: FactorUnit
    dependencies: tuple[str, ...]
    point_in_time_safety: PointInTimeSafety
    description: str


FACTOR_CATALOG: dict[FactorName, FactorDefinition] = {
    FactorName.MARKET_CAP: FactorDefinition(
        name=FactorName.MARKET_CAP,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.DAILY,
        unit=FactorUnit.CNY,
        dependencies=("daily_metrics.total_market_cap",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="总市值,单位为人民币元。",
    ),
    FactorName.PB: FactorDefinition(
        name=FactorName.PB,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.DAILY,
        unit=FactorUnit.MULTIPLE,
        dependencies=("daily_metrics.pb",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="市净率。",
    ),
    FactorName.TURNOVER_RATE: FactorDefinition(
        name=FactorName.TURNOVER_RATE,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.DAILY,
        unit=FactorUnit.RATIO,
        dependencies=("daily_metrics.turnover_rate",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="换手率,以小数表示。",
    ),
    FactorName.MOMENTUM: FactorDefinition(
        name=FactorName.MOMENTUM,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.DAILY,
        unit=FactorUnit.RATIO,
        dependencies=("bars.close",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="决策日收盘价相对指定回看窗口起点的收益率。",
    ),
    FactorName.ROE: FactorDefinition(
        name=FactorName.ROE,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.REPORT,
        unit=FactorUnit.RATIO,
        dependencies=("financial_indicators.return_on_equity",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="最近已公告财务修订的净资产收益率。",
    ),
    FactorName.GROSS_PROFIT_MARGIN: FactorDefinition(
        name=FactorName.GROSS_PROFIT_MARGIN,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.REPORT,
        unit=FactorUnit.RATIO,
        dependencies=("financial_indicators.gross_profit_margin",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="最近已公告财务修订的毛利率。",
    ),
    FactorName.REVENUE_YOY: FactorDefinition(
        name=FactorName.REVENUE_YOY,
        version=FACTOR_VERSION,
        frequency=FactorFrequency.REPORT,
        unit=FactorUnit.RATIO,
        dependencies=("financial_indicators.revenue_yoy",),
        point_in_time_safety=PointInTimeSafety.STRICT,
        description="最近已公告财务修订的营业收入同比增速。",
    ),
}


@dataclass(frozen=True, slots=True)
class FactorSelectionConfig:
    """按日选股规则;默认关闭以保持旧回测行为。"""

    enabled: bool = False
    source: str = "tushare"
    factor_version: str = FACTOR_VERSION
    max_symbols: int = 20
    ranking_factor: FactorName = FactorName.MARKET_CAP
    ranking_scope: RankingScope = RankingScope.GLOBAL
    ranking_ascending: bool = False
    max_per_industry: int | None = None
    min_listing_days: int = 60
    exclude_st: bool = True
    exclude_suspended: bool = True
    momentum_lookback: int = 20
    min_market_cap: Decimal | None = None
    max_market_cap: Decimal | None = None
    min_pb: Decimal | None = None
    max_pb: Decimal | None = None
    min_turnover_rate: Decimal | None = None
    max_turnover_rate: Decimal | None = None
    min_momentum: Decimal | None = None
    min_roe: Decimal | None = None
    min_gross_profit_margin: Decimal | None = None
    min_revenue_yoy: Decimal | None = None
    dataset_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.factor_version != FACTOR_VERSION:
            raise ValueError(f"不支持的 factor_version: {self.factor_version}")
        if not self.source.strip():
            raise ValueError("source 不能为空")
        if self.max_symbols <= 0:
            raise ValueError("max_symbols 必须大于 0")
        if self.max_per_industry is not None and self.max_per_industry <= 0:
            raise ValueError("max_per_industry 必须大于 0")
        if self.min_listing_days < 0:
            raise ValueError("min_listing_days 不能为负数")
        if self.momentum_lookback <= 0:
            raise ValueError("momentum_lookback 必须大于 0")
        _validate_bounds("market_cap", self.min_market_cap, self.max_market_cap)
        _validate_bounds("pb", self.min_pb, self.max_pb)
        _validate_bounds(
            "turnover_rate",
            self.min_turnover_rate,
            self.max_turnover_rate,
        )

    @property
    def required_factors(self) -> frozenset[FactorName]:
        """当前规则不可缺失的因子集合。"""
        required = {self.ranking_factor}
        if self.min_market_cap is not None or self.max_market_cap is not None:
            required.add(FactorName.MARKET_CAP)
        if self.min_pb is not None or self.max_pb is not None:
            required.add(FactorName.PB)
        if self.min_turnover_rate is not None or self.max_turnover_rate is not None:
            required.add(FactorName.TURNOVER_RATE)
        if self.min_momentum is not None:
            required.add(FactorName.MOMENTUM)
        if self.min_roe is not None:
            required.add(FactorName.ROE)
        if self.min_gross_profit_margin is not None:
            required.add(FactorName.GROSS_PROFIT_MARGIN)
        if self.min_revenue_yoy is not None:
            required.add(FactorName.REVENUE_YOY)
        return frozenset(required)

    @property
    def required_datasets(self) -> frozenset[str]:
        """为状态过滤和所选因子加载最小数据集集合。"""
        datasets = {"instrument_profiles", "daily_metrics"}
        if self.required_factors & {
            FactorName.ROE,
            FactorName.GROSS_PROFIT_MARGIN,
            FactorName.REVENUE_YOY,
        }:
            datasets.add("financial_indicators")
        if (
            self.ranking_scope is RankingScope.INDUSTRY
            or self.max_per_industry is not None
        ):
            datasets.add("industry_memberships")
        return frozenset(datasets)

    def as_dict(self) -> dict[str, object]:
        """返回稳定、可归档的配置结构。"""
        return {
            "enabled": self.enabled,
            "source": self.source,
            "factor_version": self.factor_version,
            "max_symbols": self.max_symbols,
            "ranking_factor": self.ranking_factor.value,
            "ranking_scope": self.ranking_scope.value,
            "ranking_ascending": self.ranking_ascending,
            "max_per_industry": self.max_per_industry,
            "min_listing_days": self.min_listing_days,
            "exclude_st": self.exclude_st,
            "exclude_suspended": self.exclude_suspended,
            "momentum_lookback": self.momentum_lookback,
            "min_market_cap": _decimal_text(self.min_market_cap),
            "max_market_cap": _decimal_text(self.max_market_cap),
            "min_pb": _decimal_text(self.min_pb),
            "max_pb": _decimal_text(self.max_pb),
            "min_turnover_rate": _decimal_text(self.min_turnover_rate),
            "max_turnover_rate": _decimal_text(self.max_turnover_rate),
            "min_momentum": _decimal_text(self.min_momentum),
            "min_roe": _decimal_text(self.min_roe),
            "min_gross_profit_margin": _decimal_text(
                self.min_gross_profit_margin
            ),
            "min_revenue_yoy": _decimal_text(self.min_revenue_yoy),
            "dataset_versions": dict(sorted(self.dataset_versions.items())),
        }


@dataclass(frozen=True, slots=True)
class FactorInputRecord:
    """一个候选标的在决策时点可见的研究数据。"""

    symbol: str
    profile: InstrumentProfile | None
    daily: DailySecurityMetrics | None
    financial: FinancialIndicator | None
    industry: IndustryMembership | None


@dataclass(frozen=True, slots=True)
class FactorInputBatch:
    """同一来源、同一组数据版本的横截面输入。"""

    records: tuple[FactorInputRecord, ...]
    source: str
    dataset_versions: dict[str, str]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FactorValue:
    """一个快照中的单标的因子值和横截面排名。"""

    symbol: str
    factor_name: FactorName
    value: Decimal
    global_rank: int | None
    industry_rank: int | None
    industry_code: str | None


@dataclass(frozen=True, slots=True)
class FactorSnapshot:
    """T 日决策、T+1 生效的不可变候选集快照。"""

    decision_at: datetime
    business_date: date
    effective_date: date
    source: str
    dataset_versions: dict[str, str]
    factor_version: str
    static_universe: tuple[str, ...]
    selected_symbols: tuple[str, ...]
    values: tuple[FactorValue, ...]
    status: FactorSnapshotStatus
    skip_reason: str | None
    config: dict[str, object]
    checksum: str
    snapshot_id: int | None = None


@runtime_checkable
class FactorResearchReader(Protocol):
    """批量读取同一决策时点的研究数据。"""

    async def load_factor_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        source: str,
        required_datasets: frozenset[str],
        dataset_versions: dict[str, str],
    ) -> FactorInputBatch:
        """返回严格满足 ``available_at <= decision_at`` 的横截面。"""
        ...


@runtime_checkable
class FactorSnapshotWriter(Protocol):
    """保存或复用不可变因子快照。"""

    async def save_factor_snapshot(self, snapshot: FactorSnapshot) -> int:
        """返回持久化快照 ID。"""
        ...


def factor_catalog() -> tuple[FactorDefinition, ...]:
    """返回按名称稳定排序的因子目录。"""
    return tuple(FACTOR_CATALOG[name] for name in sorted(FACTOR_CATALOG))


def _validate_bounds(
    name: str,
    minimum: Decimal | None,
    maximum: Decimal | None,
) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{name} 最小值不能大于最大值")


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
