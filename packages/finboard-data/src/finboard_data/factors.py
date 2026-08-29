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

from finboard_data.factor_lab import (
    FACTOR_LAB_CATALOG as _LAB_CATALOG,
)
from finboard_data.factor_lab import (
    FeatureFrequency as _LabFrequency,
)
from finboard_data.factor_lab import FeatureObservation
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
    VOLATILITY_20D = "volatility_20d"
    ROE = "roe"
    GROSS_PROFIT_MARGIN = "gross_profit_margin"
    REVENUE_YOY = "revenue_yoy"


class InputsMode(StrEnum):
    """选股因子输入的来源模式(issue #173)。

    * ``RESEARCH_DB``(默认):从 research 数据表读取 profile / daily_metrics /
      financial_indicators / industry_memberships;
    * ``BARS``:纯价格因子从回测行情历史计算(动量 / 波动率),不要求
      daily_metrics;instrument_profiles 缺失时 ST / 上市天数过滤降级为不生效;
    * ``SNAPSHOT``:因子值直接来自冻结的 ``FeatureSnapshot`` 观测。
    """

    RESEARCH_DB = "research_db"
    BARS = "bars"
    SNAPSHOT = "snapshot"


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


def _project_v1_catalog() -> dict[FactorName, FactorDefinition]:
    """从 ``FACTOR_LAB_CATALOG`` 投影生成 v1 选股规则目录(issue #214)。

    因子定义(依赖字段 / 频率 / 经济假设)唯一注册点是 v2 的
    ``FACTOR_LAB_CATALOG``;本目录只补 v1 selection 契约所需的展示映射
    (原始值单位 cny/multiple/ratio——v2 目录登记的是标准化后的 z_score
    单位)。v1 名称在 v2 目录缺失时导入期即失败,防止两套目录漂移。
    """

    units: dict[FactorName, FactorUnit] = {
        FactorName.MARKET_CAP: FactorUnit.CNY,
        FactorName.PB: FactorUnit.MULTIPLE,
        FactorName.TURNOVER_RATE: FactorUnit.RATIO,
        FactorName.MOMENTUM: FactorUnit.RATIO,
        FactorName.VOLATILITY_20D: FactorUnit.RATIO,
        FactorName.ROE: FactorUnit.RATIO,
        FactorName.GROSS_PROFIT_MARGIN: FactorUnit.RATIO,
        FactorName.REVENUE_YOY: FactorUnit.RATIO,
    }
    frequencies = {
        _LabFrequency.DAILY: FactorFrequency.DAILY,
        _LabFrequency.REPORT: FactorFrequency.REPORT,
        _LabFrequency.EVENT: FactorFrequency.REPORT,
    }
    catalog: dict[FactorName, FactorDefinition] = {}
    for name in FactorName:
        try:
            source = _LAB_CATALOG[name.value]
        except KeyError as exc:
            raise RuntimeError(
                f"v1 选股因子 {name.value} 在 FACTOR_LAB_CATALOG 中不存在;"
                "因子目录已收敛为 v2 唯一事实来源,请先在 factor_lab 登记该因子"
            ) from exc
        catalog[name] = FactorDefinition(
            name=name,
            version=FACTOR_VERSION,
            frequency=frequencies[source.frequency],
            unit=units[name],
            dependencies=source.source_fields,
            point_in_time_safety=PointInTimeSafety.STRICT,
            description=source.economic_hypothesis,
        )
    return catalog


FACTOR_CATALOG: dict[FactorName, FactorDefinition] = _project_v1_catalog()


@dataclass(frozen=True, slots=True)
class FactorSelectionConfig:
    """按日选股规则;默认关闭以保持旧回测行为。

    ``factor_version`` 是**选股规则目录版本**(本文件 ``FACTOR_VERSION``,
    目前仅 ``"v1"``)。因子定义自 ``FACTOR_LAB_CATALOG`` 投影而来(issue
    #214,唯一事实来源),可用因子集即 ``FACTOR_CATALOG`` 的键;特征快照的
    ``framework_version`` 是快照 schema 版本,与本字段无关。
    """

    enabled: bool = False
    source: str = "tushare"
    inputs_mode: InputsMode = InputsMode.RESEARCH_DB
    snapshot_ids: tuple[str, ...] = ()
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
            raise ValueError(
                f"不支持的 factor_version: {self.factor_version},"
                f"合法值仅 [{FACTOR_VERSION}]"
            )
        if not self.source.strip():
            raise ValueError("source 不能为空")
        if self.inputs_mode is InputsMode.SNAPSHOT and not self.snapshot_ids:
            raise ValueError("snapshot 输入模式必须指定 snapshot_ids")
        if self.snapshot_ids and self.inputs_mode is not InputsMode.SNAPSHOT:
            raise ValueError("snapshot_ids 仅在 snapshot 输入模式下使用")
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("snapshot_ids 不能重复")
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
        """为状态过滤和所选因子加载最小数据集集合(issue #173)。

        数据集按 ``required_factors`` 的依赖推导,而不是无条件包含
        ``daily_metrics``:纯价格因子配置(momentum / volatility)不再要求
        daily_metrics 已发布。``instrument_profiles`` 仅 research_db 模式必选;
        bars / snapshot 模式下降级为可选(缺失只记录警告)。
        """
        datasets = (
            {"instrument_profiles"}
            if self.inputs_mode is InputsMode.RESEARCH_DB
            else set()
        )
        daily_dependent = self.required_factors & {
            FactorName.MARKET_CAP,
            FactorName.PB,
            FactorName.TURNOVER_RATE,
        }
        if daily_dependent:
            datasets.add("daily_metrics")
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
            "inputs_mode": self.inputs_mode.value,
            "snapshot_ids": list(self.snapshot_ids),
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
    """一个候选标的在决策时点可见的研究数据。

    ``features`` 仅在 snapshot 输入模式下非空:因子值直接来自冻结快照观测,
    profile / daily / financial 恒为 None。
    """

    symbol: str
    profile: InstrumentProfile | None
    daily: DailySecurityMetrics | None
    financial: FinancialIndicator | None
    industry: IndustryMembership | None
    features: tuple[FeatureObservation, ...] = ()


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
    warnings: tuple[str, ...] = ()


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
