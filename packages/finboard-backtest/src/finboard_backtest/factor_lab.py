"""统一因子实验室的离线计算实现(issue #78)。

流程为 frozen release -> FeatureSnapshot -> alpha/risk/input analysis ->
FactorSignal。模块只读历史研究数据,不导入 Broker、OrderManager、PositionManager
或实盘 RiskManager。
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
import numpy.typing as npt
import structlog

from finboard_backtest.factors.extract import extract_factor_matrix
from finboard_backtest.factors.standardize import zscore
from finboard_backtest.portfolio.covariance import (
    CovarianceError,
    estimate_covariance,
)
from finboard_data.cache import ParquetCache
from finboard_data.factor_lab import (
    FactorRole,
    FeatureMissingPolicy,
    FeatureObservation,
    FeatureSnapshot,
    build_feature_snapshot,
    get_factor_definition,
)
from finboard_data.factors import FactorInputBatch
from finboard_data.releases import (
    CloseHistoryColumns,
    FrozenReleaseProvider,
    ReleaseCapabilityError,
    ReleaseDatasetKind,
    ReleasedInstrument,
    ReleaseIntegrityError,
    ResearchDatasetRelease,
    _safe_release_artifact,
    _sha256_file,
    _timestamp_available_at,
    close_history_from_columns,
    load_dataset_release,
)
from finboard_data.research import DailySecurityMetrics, FinancialIndicator
from finboard_shared.models import Symbol
from finboard_shared.types import AssetClass, BarPeriod, Market

_EPS = 1e-12
_logger = structlog.get_logger(__name__)
_TRADING_DAYS = 252
_MARKET_SYMBOL = "__market__"


class FactorAnalysisError(RuntimeError):
    """Alpha 分析输入不满足最小样本或时点约束。"""


class RiskModelError(RuntimeError):
    """风险模型缺少必要数据或无法得到稳定估计。"""


class MarketInputError(RuntimeError):
    """跨市场输入缺失、过期或包含未来数据。"""


@dataclass(frozen=True, slots=True)
class _PriceFeatureProcessInstrument:
    """进程 worker 所需的最小标的上下文,避免重复传递整个 release。"""

    code: str
    market: Market
    asset_class: AssetClass
    artifact_path: str
    artifact_checksum: str


@dataclass(frozen=True, slots=True)
class _PriceFeatureProcessTask:
    """一个独立、可 pickle 的标的特征计算任务。"""

    code: str
    start: date
    end: date
    decision_at: datetime
    momentum_lookback: int
    volatility_windows: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PriceFeatureProcessContext:
    release_dir: Path
    source: str
    source_version: str
    period: BarPeriod
    adjustment: str
    verify_files: bool
    instruments: dict[str, _PriceFeatureProcessInstrument]


@dataclass(frozen=True, slots=True)
class _PriceFeatureSnapshotAssembly:
    """子进程中完成最终排序、PIT 校验和 checksum 的输入。"""

    dataset_release_id: str
    dataset_release_checksum: str
    decision_at: datetime
    code_version: str
    observations: tuple[FeatureObservation, ...]
    calculation_windows: dict[str, int]
    transformations: dict[str, str]
    neutralization: dict[str, tuple[str, ...]]


_PRICE_FEATURE_PROCESS_CONTEXT: _PriceFeatureProcessContext | None = None


@dataclass(frozen=True, slots=True)
class FactorPeriod:
    """一个横截面评估期。

    ``forward_returns`` 按持有期组织,键为交易日数。例如 ``{1: {...}, 5:
    {...}}``。这些收益只用于事后评价,不会写入 FeatureSnapshot。
    """

    period: date
    scores: dict[str, float]
    forward_returns: dict[int, dict[str, float]]
    transaction_costs: dict[str, float]
    market_regime: str

    def __post_init__(self) -> None:
        if not self.scores:
            raise ValueError("FactorPeriod.scores 不能为空")
        if 1 not in self.forward_returns:
            raise ValueError("forward_returns 必须包含 1 日收益")
        if not self.market_regime:
            raise ValueError("market_regime 不能为空")


@dataclass(frozen=True, slots=True)
class QuantileReturn:
    quantile: int
    gross_return: float
    cost: float
    net_return: float
    average_members: float


@dataclass(frozen=True, slots=True)
class DecayPoint:
    horizon: int
    rank_ic: float
    pearson_ic: float
    observations: int


@dataclass(frozen=True, slots=True)
class RegimeAnalysis:
    regime: str
    rank_ic: float
    pearson_ic: float
    net_long_short_return: float
    periods: int


@dataclass(frozen=True, slots=True)
class AlphaAnalysisReport:
    """单 alpha 因子的完整收益相关与稳健性报告。"""

    factor_name: str
    rank_ic: float
    pearson_ic: float
    rank_ic_ir: float
    pearson_ic_ir: float
    rank_ic_t_stat: float
    rank_ic_p_value: float
    quantile_returns: tuple[QuantileReturn, ...]
    gross_long_short_return: float
    net_long_short_return: float
    average_turnover: float
    decay: tuple[DecayPoint, ...]
    neighbourhood_rank_ic: dict[str, float]
    neighbourhood_worst_rank_ic: float | None
    neighbourhood_drop: float | None
    regimes: tuple[RegimeAnalysis, ...]
    n_periods: int
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "factor_name": self.factor_name,
            "rank_ic": self.rank_ic,
            "pearson_ic": self.pearson_ic,
            "rank_ic_ir": self.rank_ic_ir,
            "pearson_ic_ir": self.pearson_ic_ir,
            "rank_ic_t_stat": self.rank_ic_t_stat,
            "rank_ic_p_value": self.rank_ic_p_value,
            "quantile_returns": [
                {
                    "quantile": item.quantile,
                    "gross_return": item.gross_return,
                    "cost": item.cost,
                    "net_return": item.net_return,
                    "average_members": item.average_members,
                }
                for item in self.quantile_returns
            ],
            "gross_long_short_return": self.gross_long_short_return,
            "net_long_short_return": self.net_long_short_return,
            "average_turnover": self.average_turnover,
            "decay": [
                {
                    "horizon": item.horizon,
                    "rank_ic": item.rank_ic,
                    "pearson_ic": item.pearson_ic,
                    "observations": item.observations,
                }
                for item in self.decay
            ],
            "neighbourhood_rank_ic": dict(
                sorted(self.neighbourhood_rank_ic.items())
            ),
            "neighbourhood_worst_rank_ic": self.neighbourhood_worst_rank_ic,
            "neighbourhood_drop": self.neighbourhood_drop,
            "regimes": [
                {
                    "regime": item.regime,
                    "rank_ic": item.rank_ic,
                    "pearson_ic": item.pearson_ic,
                    "net_long_short_return": item.net_long_short_return,
                    "periods": item.periods,
                }
                for item in self.regimes
            ],
            "n_periods": self.n_periods,
            "issues": list(self.issues),
        }


def analyze_alpha_factor(
    factor_name: str,
    periods: list[FactorPeriod],
    *,
    quantiles: int = 5,
    neighbourhood_scores: dict[str, list[dict[str, float]]] | None = None,
    min_cross_section: int = 5,
) -> AlphaAnalysisReport:
    """计算 IC、分位收益、成本、衰减、参数邻域和市场状态分层。"""

    definition = get_factor_definition(factor_name)
    if definition.role is not FactorRole.ALPHA:
        raise FactorAnalysisError(f"{factor_name} 不是 alpha 因子")
    if quantiles < 2:
        raise ValueError("quantiles 至少为 2")
    if not periods:
        raise FactorAnalysisError("periods 不能为空")

    rank_ics: list[float] = []
    pearson_ics: list[float] = []
    quantile_gross: dict[int, list[float]] = {
        index: [] for index in range(1, quantiles + 1)
    }
    quantile_cost: dict[int, list[float]] = {
        index: [] for index in range(1, quantiles + 1)
    }
    quantile_size: dict[int, list[int]] = {
        index: [] for index in range(1, quantiles + 1)
    }
    decay_rank: dict[int, list[float]] = {}
    decay_pearson: dict[int, list[float]] = {}
    regime_periods: dict[str, list[FactorPeriod]] = {}
    top_members: list[set[str]] = []
    issues: list[str] = []

    for period in periods:
        one_day = period.forward_returns[1]
        rank = _correlation(period.scores, one_day, rank=True, minimum=min_cross_section)
        pearson = _correlation(
            period.scores, one_day, rank=False, minimum=min_cross_section
        )
        if rank is not None:
            rank_ics.append(rank)
        if pearson is not None:
            pearson_ics.append(pearson)
        buckets = _quantile_members(
            period.scores,
            one_day,
            quantiles=quantiles,
            minimum=min_cross_section,
        )
        if buckets is None:
            issues.append(f"{period.period.isoformat()}:insufficient_cross_section")
        else:
            for index, symbols in buckets.items():
                bucket_returns = [one_day[symbol] for symbol in symbols]
                costs = [
                    period.transaction_costs.get(symbol, 0.0)
                    for symbol in symbols
                ]
                quantile_gross[index].append(float(np.mean(bucket_returns)))
                quantile_cost[index].append(float(np.mean(costs)))
                quantile_size[index].append(len(symbols))
            top_members.append(set(buckets[quantiles]))
        for horizon, horizon_returns in sorted(period.forward_returns.items()):
            rank_decay = _correlation(
                period.scores,
                horizon_returns,
                rank=True,
                minimum=min_cross_section,
            )
            pearson_decay = _correlation(
                period.scores,
                horizon_returns,
                rank=False,
                minimum=min_cross_section,
            )
            if rank_decay is not None:
                decay_rank.setdefault(horizon, []).append(rank_decay)
            if pearson_decay is not None:
                decay_pearson.setdefault(horizon, []).append(pearson_decay)
        regime_periods.setdefault(period.market_regime, []).append(period)

    if not rank_ics or not pearson_ics:
        raise FactorAnalysisError("没有足够横截面计算 IC")

    quantile_results = tuple(
        QuantileReturn(
            quantile=index,
            gross_return=_mean_or_zero(quantile_gross[index]),
            cost=_mean_or_zero(quantile_cost[index]),
            net_return=(
                _mean_or_zero(quantile_gross[index])
                - _mean_or_zero(quantile_cost[index])
            ),
            average_members=_mean_or_zero(quantile_size[index]),
        )
        for index in range(1, quantiles + 1)
    )
    bottom = quantile_results[0]
    top = quantile_results[-1]
    gross_long_short = top.gross_return - bottom.gross_return
    # 多空组合两端都发生交易成本。
    net_long_short = gross_long_short - top.cost - bottom.cost
    mean_rank = float(np.mean(rank_ics))
    rank_std = float(np.std(rank_ics, ddof=1)) if len(rank_ics) > 1 else 0.0
    mean_pearson = float(np.mean(pearson_ics))
    pearson_std = (
        float(np.std(pearson_ics, ddof=1)) if len(pearson_ics) > 1 else 0.0
    )
    t_stat = (
        mean_rank / (rank_std / math.sqrt(len(rank_ics)))
        if rank_std > _EPS
        else 0.0
    )
    # 无 scipy 依赖时使用大样本正态近似,方法在报告文档中显式披露。
    p_value = math.erfc(abs(t_stat) / math.sqrt(2.0))

    neighbourhood = _analyse_neighbourhood(
        periods,
        neighbourhood_scores or {},
        minimum=min_cross_section,
    )
    worst_neighbourhood = min(neighbourhood.values()) if neighbourhood else None
    neighbourhood_drop = (
        mean_rank - worst_neighbourhood
        if worst_neighbourhood is not None
        else None
    )
    regimes = tuple(
        _analyse_regime(
            regime,
            grouped,
            quantiles=quantiles,
            minimum=min_cross_section,
        )
        for regime, grouped in sorted(regime_periods.items())
    )
    decay = tuple(
        DecayPoint(
            horizon=horizon,
            rank_ic=_mean_or_zero(decay_rank.get(horizon, [])),
            pearson_ic=_mean_or_zero(decay_pearson.get(horizon, [])),
            observations=len(decay_rank.get(horizon, [])),
        )
        for horizon in sorted(set(decay_rank) | set(decay_pearson))
    )
    return AlphaAnalysisReport(
        factor_name=factor_name,
        rank_ic=mean_rank,
        pearson_ic=mean_pearson,
        rank_ic_ir=mean_rank / rank_std if rank_std > _EPS else 0.0,
        pearson_ic_ir=(
            mean_pearson / pearson_std if pearson_std > _EPS else 0.0
        ),
        rank_ic_t_stat=t_stat,
        rank_ic_p_value=p_value,
        quantile_returns=quantile_results,
        gross_long_short_return=gross_long_short,
        net_long_short_return=net_long_short,
        average_turnover=_average_membership_turnover(top_members),
        decay=decay,
        neighbourhood_rank_ic=neighbourhood,
        neighbourhood_worst_rank_ic=worst_neighbourhood,
        neighbourhood_drop=neighbourhood_drop,
        regimes=regimes,
        n_periods=len(periods),
        issues=tuple(issues),
    )


@dataclass(frozen=True, slots=True)
class RiskExposure:
    symbol: str
    market_beta: float
    industry: str
    asset_class: str
    size: float
    volatility: float
    liquidity: float


@dataclass(frozen=True, slots=True)
class BasicRiskModel:
    """与 alpha 分数分离的风险暴露、协方差和风险贡献。"""

    as_of: datetime
    symbols: tuple[str, ...]
    exposures: tuple[RiskExposure, ...]
    covariance: tuple[tuple[float, ...], ...]
    covariance_method: str
    covariance_shrinkage: float
    observations: int
    portfolio_volatility: float
    marginal_risk_contribution: dict[str, float]
    component_risk_contribution: dict[str, float]
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "symbols": list(self.symbols),
            "exposures": [
                {
                    "symbol": item.symbol,
                    "market_beta": item.market_beta,
                    "industry": item.industry,
                    "asset_class": item.asset_class,
                    "size": item.size,
                    "volatility": item.volatility,
                    "liquidity": item.liquidity,
                }
                for item in self.exposures
            ],
            "covariance": [list(row) for row in self.covariance],
            "covariance_method": self.covariance_method,
            "covariance_shrinkage": self.covariance_shrinkage,
            "observations": self.observations,
            "portfolio_volatility": self.portfolio_volatility,
            "marginal_risk_contribution": dict(
                sorted(self.marginal_risk_contribution.items())
            ),
            "component_risk_contribution": dict(
                sorted(self.component_risk_contribution.items())
            ),
            "issues": list(self.issues),
        }


def estimate_basic_risk_model(
    *,
    as_of: datetime,
    returns_by_symbol: dict[str, npt.NDArray[np.float64]],
    benchmark_returns: npt.NDArray[np.float64],
    industries: dict[str, str],
    asset_classes: dict[str, str],
    market_caps: dict[str, float],
    liquidity: dict[str, float],
    portfolio_weights: dict[str, float] | None = None,
    min_observations: int = 30,
) -> BasicRiskModel:
    """估计 beta、行业/资产类别、规模、波动率、流动性和协方差。

    任何必要元数据缺失都 fail closed,不会把未知行业或资产类别静默归为
    ``other``。
    """

    _require_aware(as_of, "as_of")
    if len(benchmark_returns) < min_observations:
        raise RiskModelError("基准收益观测不足")
    symbols = tuple(sorted(returns_by_symbol))
    if not symbols:
        raise RiskModelError("returns_by_symbol 不能为空")
    for name, values in (
        ("industry", industries),
        ("asset_class", asset_classes),
        ("market_cap", market_caps),
        ("liquidity", liquidity),
    ):
        missing = set(symbols) - set(values)
        if missing:
            raise RiskModelError(f"{name} 元数据缺失: {sorted(missing)}")
    for symbol in symbols:
        if not industries[symbol] or not asset_classes[symbol]:
            raise RiskModelError(f"{symbol} 行业/资产类别为空")
        if (
            not math.isfinite(market_caps[symbol])
            or not math.isfinite(liquidity[symbol])
            or market_caps[symbol] <= 0
            or liquidity[symbol] <= 0
        ):
            raise RiskModelError(f"{symbol} 市值/流动性必须为正且有限")
    try:
        estimate = estimate_covariance(
            returns_by_symbol,
            min_observations=min_observations,
        )
    except CovarianceError as exc:
        raise RiskModelError(str(exc)) from exc
    if tuple(estimate.tickers) != symbols:
        missing_returns = set(symbols) - set(estimate.tickers)
        raise RiskModelError(f"协方差排除了标的: {sorted(missing_returns)}")

    size_z = zscore(
        {symbol: math.log(market_caps[symbol]) for symbol in symbols}
    )
    liquidity_z = zscore(
        {symbol: math.log(liquidity[symbol]) for symbol in symbols}
    )
    exposures: list[RiskExposure] = []
    for symbol in symbols:
        returns = np.asarray(returns_by_symbol[symbol], dtype=np.float64)
        aligned = min(len(returns), len(benchmark_returns))
        if aligned < min_observations:
            raise RiskModelError(f"{symbol} 与基准重叠观测不足")
        asset_returns = returns[-aligned:]
        benchmark = np.asarray(benchmark_returns[-aligned:], dtype=np.float64)
        mask = np.isfinite(asset_returns) & np.isfinite(benchmark)
        if int(mask.sum()) < min_observations:
            raise RiskModelError(f"{symbol} 与基准有效观测不足")
        benchmark_var = float(np.var(benchmark[mask], ddof=1))
        if benchmark_var <= _EPS:
            raise RiskModelError("基准方差为零,无法估计 beta")
        beta = float(np.cov(asset_returns[mask], benchmark[mask], ddof=1)[0, 1])
        beta /= benchmark_var
        volatility = float(np.std(asset_returns[mask], ddof=1))
        volatility *= math.sqrt(_TRADING_DAYS)
        exposures.append(
            RiskExposure(
                symbol=symbol,
                market_beta=beta,
                industry=industries[symbol],
                asset_class=asset_classes[symbol],
                size=size_z[symbol],
                volatility=volatility,
                liquidity=liquidity_z[symbol],
            )
        )

    (
        portfolio_volatility,
        marginal_risk_contribution,
        component_risk_contribution,
    ) = _risk_contributions(
        symbols,
        estimate.matrix,
        portfolio_weights or {},
    )
    return BasicRiskModel(
        as_of=as_of,
        symbols=symbols,
        exposures=tuple(exposures),
        covariance=tuple(
            tuple(float(value) for value in row) for row in estimate.matrix
        ),
        covariance_method=estimate.method,
        covariance_shrinkage=estimate.shrinkage,
        observations=estimate.n_observations,
        portfolio_volatility=portfolio_volatility,
        marginal_risk_contribution=marginal_risk_contribution,
        component_risk_contribution=component_risk_contribution,
    )


@dataclass(frozen=True, slots=True)
class MarketInputObservation:
    feature_name: str
    value: float
    observed_at: datetime
    available_at: datetime
    source: str
    source_version: str

    def __post_init__(self) -> None:
        definition = get_factor_definition(self.feature_name)
        if definition.role is not FactorRole.MARKET_INPUT:
            raise ValueError(f"{self.feature_name} 不是 market input")
        if not math.isfinite(self.value):
            raise ValueError("market input value 必须为有限数")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.available_at, "available_at")
        if not self.source or not self.source_version:
            raise ValueError("market input source/version 必填")


@dataclass(frozen=True, slots=True)
class CrossMarketSnapshot:
    decision_at: datetime
    features: tuple[FeatureObservation, ...]
    staleness_days: dict[str, int]
    missing_policies: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_at": self.decision_at.isoformat(),
            "features": [item.as_dict() for item in self.features],
            "staleness_days": dict(sorted(self.staleness_days.items())),
            "missing_policies": dict(sorted(self.missing_policies.items())),
        }


def build_cross_market_snapshot(
    *,
    decision_at: datetime,
    observations: list[MarketInputObservation],
    required_features: tuple[str, ...] = (
        "risk_free_rate",
        "government_bond_return",
        "fx_usdcny_return",
        "gold_return",
        "market_breadth",
        "volatility_regime",
    ),
    max_staleness_days: dict[str, int] | None = None,
) -> CrossMarketSnapshot:
    """选择 decision_at 前最后可见值并执行缺失/陈旧数据门。"""

    _require_aware(decision_at, "decision_at")
    staleness_limit = max_staleness_days or {}
    grouped: dict[str, list[MarketInputObservation]] = {}
    for observation in observations:
        if observation.available_at <= decision_at:
            grouped.setdefault(observation.feature_name, []).append(observation)
    features: list[FeatureObservation] = []
    staleness: dict[str, int] = {}
    policies: dict[str, str] = {}
    for name in required_features:
        definition = get_factor_definition(name)
        if definition.role is not FactorRole.MARKET_INPUT:
            raise MarketInputError(f"{name} 不是跨市场输入")
        policies[name] = definition.missing_policy.value
        candidates = grouped.get(name, [])
        if not candidates:
            if definition.missing_policy is FeatureMissingPolicy.FAIL_CLOSED:
                raise MarketInputError(f"缺少必要跨市场输入: {name}")
            continue
        latest = max(candidates, key=lambda item: item.available_at)
        age = max(0, (decision_at.date() - latest.available_at.date()).days)
        limit = staleness_limit.get(name, 5)
        if age > limit:
            raise MarketInputError(
                f"{name} 已过期: age={age} days limit={limit}"
            )
        staleness[name] = age
        features.append(
            FeatureObservation(
                symbol=_MARKET_SYMBOL,
                feature_name=name,
                value=latest.value,
                observed_at=latest.observed_at,
                available_at=latest.available_at,
                source=latest.source,
                source_version=latest.source_version,
                market="cross_market",
                asset_class="macro",
            )
        )
    return CrossMarketSnapshot(
        decision_at=decision_at,
        features=tuple(sorted(features, key=lambda item: item.feature_name)),
        staleness_days=staleness,
        missing_policies=policies,
    )


async def build_cross_section_feature_snapshot_from_releases(
    *,
    releases: Sequence[ResearchDatasetRelease],
    providers: Mapping[str, FrozenReleaseProvider],
    decision_at: datetime,
    code_version: str,
    momentum_lookback: int = 20,
    volatility_windows: tuple[int, ...] = (20, 60, 120),
    max_concurrency: int = 8,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> FeatureSnapshot:
    """从联合冻结发布构建横截面因子快照(issue #187)。

    ``bars`` 主发布提供价格特征(momentum / volatility),``daily_metrics`` /
    ``financial_indicators`` 发布提供估值 / 流动性 / 财务因子(pb / 市值 /
    换手 / ROE / 毛利率等),观测按 ``available_at <= decision_at`` PIT 门控。
    与 ``build_price_feature_snapshot`` 共享 ``build_cross_section_feature_snapshot``
    的因子定义,保证快照产出与信号引擎消费口径一致。

    返回的 ``FeatureSnapshot`` 绑定 bars 主发布(候选池来源);研究数据观测的
    ``source_artifact_ids`` 语义由调用方(冻结快照任务)保留。
    """
    bars_provider = _bars_provider(providers, releases)
    release = bars_provider.release
    _require_aware(decision_at, "decision_at")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")
    total = len(release.instruments)
    if total == 0:
        raise FactorAnalysisError("联合发布没有可计算的标的")

    from finboard_data.factors import FactorInputBatch, FactorInputRecord

    metrics_provider = _kind_provider(
        providers, ReleaseDatasetKind.DAILY_METRICS
    )
    financial_provider = _kind_provider(
        providers, ReleaseDatasetKind.FINANCIAL_INDICATORS
    )
    semaphore = asyncio.Semaphore(max_concurrency)
    records: dict[str, FactorInputRecord] = {}
    price_history: dict[str, list[float]] = {}
    price_available_at: dict[str, datetime] = {}
    done_count = 0
    # issue #212:bars 主发布与附加研究发布的标的集天然不完全一致(次新股尚无
    # 财报、元数据未登记等),研究发布缺标的 → 该标的基本面因子为 null 并计
    # 数进快照 issues——这不清算为「回退外部数据源」,与 record.daily is None
    # 的既有语义一致;bars 主发布缺标的仍 fail-closed。
    missing_in_research: dict[str, int] = {}

    def _tolerate_missing(kind: str) -> None:
        missing_in_research[kind] = missing_in_research.get(kind, 0) + 1

    async def _load_one(instrument: ReleasedInstrument) -> None:
        nonlocal done_count
        symbol = Symbol(code=instrument.code, market=instrument.market)
        daily = None
        financial = None
        if metrics_provider is not None:
            try:
                daily = await _latest_daily_metric(
                    metrics_provider, symbol, decision_at
                )
            except ReleaseCapabilityError:
                _tolerate_missing("daily_metrics")
        if financial_provider is not None:
            try:
                financial = await _latest_financial(
                    financial_provider, symbol, decision_at
                )
            except ReleaseCapabilityError:
                _tolerate_missing("financial_indicators")
        columns = await bars_provider.fetch_close_history(
            symbol,
            release.period,
            release.start_date,
            min(decision_at.date(), release.end_date),
            decision_at=decision_at,
            adjust=release.adjustment,
        )
        records[instrument.code] = FactorInputRecord(
            symbol=instrument.code,
            profile=None,
            daily=daily,
            financial=financial,
            industry=None,
        )
        # issue #300:列式直出的 close 序列,to_list() 与旧逐行
        # [float(item.close)...] 逐值相等。
        closes = columns.closes.tolist()
        if len(closes) >= 2:
            price_history[instrument.code] = closes
            price_available_at[instrument.code] = columns.available_at[-1]
        done_count += 1
        if on_progress is not None:
            on_progress(instrument.code, done_count, total)

    async def _worker() -> None:
        while True:
            try:
                instrument = _next_instrument()
            except asyncio.QueueEmpty:
                return
            async with semaphore:
                await _load_one(instrument)

    queue: asyncio.Queue[ReleasedInstrument] = asyncio.Queue()
    for instrument in release.instruments:
        queue.put_nowait(instrument)

    def _next_instrument() -> ReleasedInstrument:
        return queue.get_nowait()

    worker_count = min(max_concurrency, total)
    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
    await asyncio.gather(*workers)
    batch = FactorInputBatch(
        records=tuple(records[item.code] for item in release.instruments),
        source=release.source,
        dataset_versions={"release": release.release_id},
        issues=tuple(
            f"missing_in_research_release:{kind}:{count}"
            for kind, count in sorted(missing_in_research.items())
            if count
        ),
    )
    return build_cross_section_feature_snapshot(
        release=release,
        batch=batch,
        decision_at=decision_at,
        code_version=code_version,
        price_history=price_history,
        price_available_at=price_available_at,
        momentum_lookback=momentum_lookback,
        volatility_windows=volatility_windows,
    )


def _bars_provider(
    providers: Mapping[str, FrozenReleaseProvider],
    releases: Sequence[ResearchDatasetRelease],
) -> FrozenReleaseProvider:
    bars = [
        providers[release.release_id]
        for release in releases
        if release.dataset_kind is ReleaseDatasetKind.BARS
    ]
    if len(bars) != 1:
        raise FactorAnalysisError(
            "联合发布必须恰好包含一个 bars 主发布,实际: "
            + ",".join(release.dataset_kind.value for release in releases)
        )
    return bars[0]


def _kind_provider(
    providers: Mapping[str, FrozenReleaseProvider],
    kind: ReleaseDatasetKind,
) -> FrozenReleaseProvider | None:
    for provider in providers.values():
        if provider.release.dataset_kind is kind:
            return provider
    return None


async def _latest_daily_metric(
    provider: FrozenReleaseProvider,
    symbol: Symbol,
    decision_at: datetime,
) -> DailySecurityMetrics | None:
    """取 daily_metrics 发布在决策时点可见的最新一条记录。"""
    records = await provider.fetch_daily_metrics(
        symbol,
        start=provider.release.start_date,
        end=decision_at.date(),
        decision_at=decision_at,
    )
    if not records:
        return None
    return sorted(records, key=lambda item: item.available_at)[-1]


async def _latest_financial(
    provider: FrozenReleaseProvider,
    symbol: Symbol,
    decision_at: datetime,
) -> FinancialIndicator | None:
    """取 financial_indicators 发布在决策时点可见的最新公告修订。"""
    records = await provider.fetch_financial_indicators(
        symbol,
        decision_at=decision_at,
    )
    if not records:
        return None
    return sorted(records, key=lambda item: item.available_at)[-1]


def build_cross_section_feature_snapshot(
    *,
    release: ResearchDatasetRelease,
    batch: FactorInputBatch,
    decision_at: datetime,
    code_version: str,
    price_history: dict[str, list[float]] | None = None,
    price_available_at: dict[str, datetime] | None = None,
    momentum_lookback: int = 20,
    volatility_windows: tuple[int, ...] = (20, 60, 120),
) -> FeatureSnapshot:
    """把 A 股时点化横截面输入转换为统一 FeatureSnapshot。"""

    if not release.is_usable:
        raise FactorAnalysisError(f"数据发布不可用: {release.release_id}")
    matrix = extract_factor_matrix(
        batch,
        price_history=price_history,
        momentum_lookback=momentum_lookback,
        volatility_windows=volatility_windows,
    )
    record_by_symbol = {record.symbol: record for record in batch.records}
    # 快照观测白名单:价格因子 + 研究数据因子。市值用 ``market_cap``
    # (universe 过滤的纯 feature_id,无需 FACTOR_LAB_CATALOG 注册)。
    _observable_factors = frozenset(
        {
            "pb",
            "market_cap",
            "earnings_yield",
            "dividend_yield",
            "roe",
            "gross_profit_margin",
            "debt_to_assets",
            "revenue_yoy",
            "momentum",
            "volatility_20d",
            "volatility_60d",
            "volatility_120d",
            "downside_volatility",
            "turnover_rate",
        }
    )
    observations: list[FeatureObservation] = []
    for factor_name, values in sorted(matrix.items()):
        if factor_name not in _observable_factors:
            continue
        for symbol, value in sorted(values.items()):
            record = record_by_symbol.get(symbol)
            if record is None:
                continue
            if factor_name in {
                "pb",
                "market_cap",
                "earnings_yield",
                "dividend_yield",
                "turnover_rate",
            }:
                if record.daily is None:
                    continue
                observed_at = record.daily.observed_at
                available_at = record.daily.available_at
                source = record.daily.source
            elif factor_name in {
                "roe",
                "gross_profit_margin",
                "debt_to_assets",
                "revenue_yoy",
            }:
                if record.financial is None:
                    continue
                observed_at = record.financial.observed_at
                available_at = record.financial.available_at
                source = record.financial.source
            else:
                if price_available_at is None or symbol not in price_available_at:
                    raise FactorAnalysisError(
                        f"{symbol}/{factor_name} 缺少 price_available_at"
                    )
                observed_at = price_available_at[symbol]
                available_at = price_available_at[symbol]
                source = release.source
            industry = (
                record.industry.level1_code if record.industry is not None else None
            )
            observations.append(
                FeatureObservation(
                    symbol=symbol,
                    feature_name=factor_name,
                    value=value,
                    observed_at=observed_at,
                    available_at=available_at,
                    source=source,
                    source_version=release.version,
                    market="a_share",
                    asset_class="equity",
                    industry=industry,
                )
            )
    windows = {"momentum": momentum_lookback}
    windows.update(
        {f"volatility_{window}d": window for window in volatility_windows}
    )
    windows["downside_volatility"] = 60
    return build_feature_snapshot(
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        decision_at=decision_at,
        code_version=code_version,
        observations=observations,
        calculation_windows={
            name: value
            for name, value in windows.items()
            if any(item.feature_name == name for item in observations)
        },
        transformations={
            item.feature_name: _snapshot_definition_transform(item.feature_name)
            for item in observations
        },
        neutralization={
            item.feature_name: _snapshot_definition_neutralization(item.feature_name)
            for item in observations
        },
        issues=tuple(batch.issues),
    )


def _snapshot_definition_transform(feature_name: str) -> str:
    """取观测因子的默认变换;未注册的 feature_id(如 market_cap)用 raw。"""
    from finboard_data.factor_lab import get_factor_definition

    try:
        return get_factor_definition(feature_name).default_transform
    except KeyError:
        return "raw"


def _snapshot_definition_neutralization(feature_name: str) -> tuple[str, ...]:
    """取观测因子的默认中性化;未注册的 feature_id 不中性化。"""
    from finboard_data.factor_lab import get_factor_definition

    try:
        return get_factor_definition(feature_name).default_neutralization
    except KeyError:
        return ()


def _build_price_observations(
    *,
    source: str,
    source_version: str,
    symbol: str,
    market: Market,
    asset_class: AssetClass,
    columns: CloseHistoryColumns,
    momentum_lookback: int,
    volatility_windows: tuple[int, ...],
) -> list[FeatureObservation]:
    """把单个标的的列式 PIT close(#300)转换为价格特征观测。

    与对象路径逐值等价:``closes`` float64 直出与
    ``np.asarray([float(item.close) for item in points])`` 逐值相等,
    ``last_timestamp`` / ``last available_at`` 即旧路径 ``points[-1]`` 的
    timestamp / available_at,特征数学段不变。
    """

    closes = columns.closes
    if closes.size == 0:
        return []
    returns = np.diff(closes) / closes[:-1]
    last_timestamp = columns.last_timestamp
    last_available_at = columns.available_at[-1]
    assert last_timestamp is not None

    def _observation(feature_name: str, value: float) -> FeatureObservation:
        return FeatureObservation(
            symbol=symbol,
            feature_name=feature_name,
            value=value,
            observed_at=last_timestamp,
            available_at=last_available_at,
            source=source,
            source_version=source_version,
            market=market.value,
            asset_class=asset_class.value,
        )

    observations: list[FeatureObservation] = []
    if len(closes) >= momentum_lookback + 1:
        observations.append(
            _observation(
                "momentum",
                float(closes[-1] / closes[-momentum_lookback - 1] - 1.0),
            )
        )
    for window in volatility_windows:
        if len(returns) >= window:
            observations.append(
                _observation(
                    f"volatility_{window}d",
                    float(np.std(returns[-window:], ddof=1)),
                )
            )
    downside_window = min(60, len(returns))
    if downside_window >= 10:
        downside = returns[-downside_window:]
        downside = downside[downside < 0]
        if len(downside) >= 3:
            observations.append(
                _observation(
                    "downside_volatility",
                    float(np.std(downside, ddof=1)),
                )
            )
    return observations


def _init_price_feature_process(
    release_dir: str,
    release_id: str,
    verify_files: bool,
    expected_checksum: str | None = None,
) -> None:
    """初始化独立进程的只读研究上下文。"""

    global _PRICE_FEATURE_PROCESS_CONTEXT
    release = load_dataset_release(
        Path(release_dir),
        expected_checksum=expected_checksum,
    )
    if release.release_id != release_id:
        raise RuntimeError("特征计算进程的 release_id 不一致")
    if not release.is_usable:
        raise RuntimeError(
            f"特征计算进程的发布不可用: {release_id}"
        )
    _PRICE_FEATURE_PROCESS_CONTEXT = _PriceFeatureProcessContext(
        release_dir=Path(release_dir),
        source=release.source,
        source_version=release.version,
        period=release.period,
        adjustment=release.adjustment,
        verify_files=verify_files,
        instruments={
            item.code: _PriceFeatureProcessInstrument(
                code=item.code,
                market=item.market,
                asset_class=item.asset_class,
                artifact_path=item.artifact_path,
                artifact_checksum=item.artifact_checksum,
            )
            for item in release.instruments
        },
    )


def _compute_price_feature_process_task(
    task: _PriceFeatureProcessTask,
) -> tuple[str, list[FeatureObservation]]:
    """在子进程中读取一个标的并完成全部价格特征计算。"""

    context = _PRICE_FEATURE_PROCESS_CONTEXT
    if context is None:
        raise RuntimeError("特征计算进程未初始化")
    item = context.instruments.get(task.code)
    if item is None:
        raise FactorAnalysisError(f"进程 worker 找不到标的: {task.code}")

    artifact = _safe_release_artifact(context.release_dir, item.artifact_path)
    if context.verify_files:
        actual = _sha256_file(artifact)
        if actual != item.artifact_checksum:
            raise ReleaseIntegrityError(
                f"{task.code} 文件校验和不一致: expected={item.artifact_checksum} "
                f"actual={actual}"
            )

    symbol = Symbol(task.code, item.market)
    if context.period is BarPeriod.D1:
        columns = close_history_from_columns(
            ParquetCache.read_close_columns_sync(
                artifact,
                symbol,
                context.period,
                task.start,
                task.end,
            ),
            market=item.market,
            decision_at=task.decision_at,
        )
    else:
        bars = ParquetCache.read_bars_sync(artifact, symbol, context.period)
        visible = [
            bar
            for bar in bars
            if task.start <= bar.timestamp.date() <= task.end
            and _timestamp_available_at(bar.timestamp, context.period, item.market)
            <= task.decision_at
        ]
        columns = CloseHistoryColumns(
            dates=tuple(bar.timestamp.date() for bar in visible),
            available_at=tuple(
                _timestamp_available_at(bar.timestamp, context.period, item.market)
                for bar in visible
            ),
            closes=np.array(
                [float(bar.close) for bar in visible], dtype=np.float64
            ),
            last_timestamp=visible[-1].timestamp if visible else None,
        )

    observations = _build_price_observations(
        source=context.source,
        source_version=context.source_version,
        symbol=item.code,
        market=item.market,
        asset_class=item.asset_class,
        columns=columns,
        momentum_lookback=task.momentum_lookback,
        volatility_windows=task.volatility_windows,
    )
    return task.code, observations


def _price_feature_process_warmup() -> None:
    """让进程池完成 import/initializer 预热;不执行研究计算。"""


def _assemble_price_feature_snapshot(
    assembly: _PriceFeatureSnapshotAssembly,
) -> FeatureSnapshot:
    """在计算进程中完成最终快照构建,避免 API 进程执行大段 JSON/hash。"""

    return build_feature_snapshot(
        dataset_release_id=assembly.dataset_release_id,
        dataset_release_checksum=assembly.dataset_release_checksum,
        decision_at=assembly.decision_at,
        code_version=assembly.code_version,
        observations=assembly.observations,
        calculation_windows=assembly.calculation_windows,
        transformations=assembly.transformations,
        neutralization=assembly.neutralization,
    )


def _create_price_feature_process_executor(
    *,
    worker_count: int,
    provider: FrozenReleaseProvider,
    max_tasks_per_child: int | None = None,
) -> tuple[ProcessPoolExecutor, tuple[Future[None], ...]]:
    """创建并提交预热任务;调用方应在线程中执行本函数。"""

    release = provider.release
    executor = ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_price_feature_process,
        initargs=(
            str(provider.release_dir),
            release.release_id,
            provider.verify_files,
            release.release_checksum,
        ),
        # issue #288:常驻池每个 worker 执行 N 个任务后重启一次,既把 Windows
        # spawn 的 import/initializer 开销摊销到多次调用,又防止长跑进程的
        # 内存累积(spawn 上下文才支持本参数;fork 不支持,本项目只在 spawn 用池)。
        max_tasks_per_child=max_tasks_per_child,
    )
    warmups = tuple(
        executor.submit(_price_feature_process_warmup)
        for _ in range(worker_count)
    )
    return executor, warmups


#: 常驻池(:class:`PriceFeatureProcessPool`)每个 worker 进程重启前的任务数
#: (issue #288)。取值权衡:过小则 spawn 重启频繁(Windows 每次约 1-2s),
#: 过大则失去防内存累积意义;128 ≈ 数个决策期 x 数十标的的任务量。
PRICE_FEATURE_POOL_MAX_TASKS_PER_CHILD = 128


class PriceFeatureProcessPool:
    """跨多次价格特征计算复用的常驻 spawn 进程池(issue #288)。

    research_run 多期回放逐期重算价格特征时,若每期各自 ``build_price_feature_
    snapshot(process_workers>0)``,则每期都要付出一次「建池 + spawn import +
    关池」的开销(N 期 = N 倍)。本句柄把池的生命周期提升到「一次加载期」:
    ``start`` 一次建池 + 预热,期内全部期共享,``aclose`` 在加载结束(或异常)
    后统一关闭。``max_tasks_per_child`` 令 worker 定期重启,防内存累积。

    池以给定 provider 的冻结发布初始化(``_init_price_feature_process``)——
    调用方必须保证传给 ``build_price_feature_snapshot(process_executor=...)``
    的 provider 与本池的 provider 是同一发布(checksum 锚定一致),否则结果
    无意义;research_run 路径天然满足(逐期特征只读 bars 主发布)。
    """

    def __init__(
        self,
        *,
        provider: FrozenReleaseProvider,
        worker_count: int,
        max_tasks_per_child: int = PRICE_FEATURE_POOL_MAX_TASKS_PER_CHILD,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count 必须 >= 1")
        self._provider = provider
        self._worker_count = worker_count
        self._max_tasks_per_child = max_tasks_per_child
        self._executor: ProcessPoolExecutor | None = None
        self._broken = False

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def broken(self) -> bool:
        """池已在中途损坏(BrokenProcessPool),不应再提交任务。"""
        return self._broken

    def mark_broken(self) -> None:
        self._broken = True

    @property
    def executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            raise RuntimeError("PriceFeatureProcessPool 尚未 start()")
        return self._executor

    async def start(self) -> None:
        """创建进程池并预热全部 worker(阻塞操作放线程,不卡事件循环)。

        initializer / 预热失败(spawn 环境损坏、发布不可读等)在此抛出,由
        调用方决定降级;失败后池保持未启动状态。
        """
        if self._executor is not None:
            return
        executor, warmups = await asyncio.to_thread(
            _create_price_feature_process_executor,
            worker_count=self._worker_count,
            provider=self._provider,
            max_tasks_per_child=self._max_tasks_per_child,
        )
        try:
            await asyncio.gather(
                *(asyncio.wrap_future(warmup) for warmup in warmups)
            )
        except BaseException:
            await asyncio.to_thread(
                executor.shutdown, wait=True, cancel_futures=True
            )
            raise
        self._executor = executor

    async def aclose(self) -> None:
        """关闭池(shutdown 是同步 API,放线程避免卡事件循环)。幂等。

        shutdown 异常(如池已 BrokenProcessPool 后的收尾失败)只记 debug 日志
        不上抛 —— 生命周期清理不得掩盖调用方的业务结果 / 异常。
        """
        if self._executor is None:
            return
        executor, self._executor = self._executor, None
        try:
            await asyncio.to_thread(
                executor.shutdown, wait=True, cancel_futures=True
            )
        except Exception:
            _logger.debug(
                "price_feature_pool.shutdown_failed",
                exc_info=True,
            )

    async def __aenter__(self) -> PriceFeatureProcessPool:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


async def _build_price_feature_snapshot_in_processes(
    *,
    provider: FrozenReleaseProvider,
    decision_at: datetime,
    code_version: str,
    momentum_lookback: int,
    volatility_windows: tuple[int, ...],
    process_workers: int,
    on_progress: Callable[[str, int, int], None] | None,
    symbols: Sequence[str] | None = None,
    external_executor: ProcessPoolExecutor | None = None,
) -> FeatureSnapshot:
    """使用独立 spawn 进程计算价格特征,不占用 API 进程的 GIL。

    ``external_executor``(issue #288)非空时复用调用方管理的常驻池(必须以
    同一发布初始化),本函数不再建池 / 预热 / 关池;为 None 时自建自毁(既有
    行为,单次调用场景)。
    """

    release = provider.release
    instruments = _scoped_instruments(release, symbols)
    total = len(instruments)
    worker_count = min(process_workers, total)
    tasks = asyncio.Queue[_PriceFeatureProcessTask]()
    for item in instruments:
        tasks.put_nowait(
            _PriceFeatureProcessTask(
                code=item.code,
                start=release.start_date,
                end=min(decision_at.date(), release.end_date),
                decision_at=decision_at,
                momentum_lookback=momentum_lookback,
                volatility_windows=volatility_windows,
            )
        )

    executor = external_executor
    if executor is None:
        executor, warmups = await asyncio.to_thread(
            _create_price_feature_process_executor,
            worker_count=worker_count,
            provider=provider,
        )
    # 自建 / 外部常驻池二选一,此处必非 None(供闭包与 finally 收窄)。
    assert executor is not None
    loop = asyncio.get_running_loop()
    observations_by_symbol: dict[str, list[FeatureObservation]] = {}
    done_count = 0

    async def _worker() -> None:
        nonlocal done_count
        while True:
            try:
                task = tasks.get_nowait()
            except asyncio.QueueEmpty:
                return
            code, observations = await loop.run_in_executor(
                executor,
                _compute_price_feature_process_task,
                task,
            )
            observations_by_symbol[code] = observations
            done_count += 1
            if on_progress is not None:
                on_progress(code, done_count, total)

    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
    snapshot: FeatureSnapshot | None = None
    try:
        try:
            if external_executor is None:
                await asyncio.gather(
                    *(asyncio.wrap_future(warmup) for warmup in warmups)
                )
            await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        observations = [
            observation
            for instrument in instruments
            for observation in observations_by_symbol[instrument.code]
        ]
        if not observations:
            raise FactorAnalysisError("冻结发布在决策时点没有足够数据计算价格特征")
        windows = {"momentum": momentum_lookback, "downside_volatility": 60}
        windows.update(
            {f"volatility_{window}d": window for window in volatility_windows}
        )
        feature_names = {item.feature_name for item in observations}
        assembly = _PriceFeatureSnapshotAssembly(
            dataset_release_id=release.release_id,
            dataset_release_checksum=release.release_checksum,
            decision_at=decision_at,
            code_version=code_version,
            observations=tuple(observations),
            calculation_windows={
                name: window
                for name, window in windows.items()
                if name in feature_names
            },
            transformations={
                name: get_factor_definition(name).default_transform
                for name in feature_names
            },
            neutralization={
                name: get_factor_definition(name).default_neutralization
                for name in feature_names
            },
        )
        snapshot = await loop.run_in_executor(
            executor,
            _assemble_price_feature_snapshot,
            assembly,
        )
    finally:
        # shutdown 是同步 API,放到线程中避免应用关闭/任务失败时再次卡住事件循环。
        # 外部常驻池(issue #288)由调用方负责生命周期,这里不关。
        if external_executor is None:
            await asyncio.to_thread(
                executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
    assert snapshot is not None
    return snapshot


def _scoped_instruments(
    release: ResearchDatasetRelease,
    symbols: Sequence[str] | None,
) -> Sequence[ReleasedInstrument]:
    """按符号清单收窄发布 instruments(issue #254)。

    ``symbols`` 为 None / 空时返回全部 instruments(既有行为);非空时只保留
    命中的标的,用于 multi_period 回放在声明 ``explicit_symbols`` 时跳过对
    发布全市场的价格特征重算(universe 过滤域之外的特征无人消费)。
    """
    instruments = release.instruments
    if not symbols:
        return instruments
    wanted = set(symbols)
    return [item for item in instruments if item.code in wanted]


async def build_price_feature_snapshot(
    *,
    provider: FrozenReleaseProvider,
    decision_at: datetime,
    code_version: str,
    momentum_lookback: int = 20,
    volatility_windows: tuple[int, ...] = (20, 60, 120),
    max_concurrency: int = 8,
    process_workers: int = 0,
    on_progress: Callable[[str, int, int], None] | None = None,
    symbols: Sequence[str] | None = None,
    process_executor: ProcessPoolExecutor | None = None,
) -> FeatureSnapshot:
    """从 #77 冻结发布构建 ETF/多资产价格特征快照。

    每个标的只返回价格特征所需的轻量数据,跨标的读取使用有界 worker;
    ``process_workers`` 大于 0 时使用独立 spawn 进程,避免大量 Parquet
    解码和 Decimal 转换阻塞 API 进程;结果仍按冻结发布顺序汇总。

    ``process_executor``(issue #288)非空时复用外部常驻池(见
    :class:`PriceFeatureProcessPool`,必须以同一发布初始化),跳过每次调用
    的建池 / 预热 / 关池开销;仅 ``process_workers > 0`` 时生效。

    ``symbols``(issue #254)非空时只重算命中标的——multi_period 回放声明
    ``explicit_symbols`` 时按声明域收窄,行为不变(过滤域之外的特征无消费方),
    每期重算成本从发布全市场降到声明规模。
    """

    _require_aware(decision_at, "decision_at")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")
    if process_workers < 0:
        raise ValueError("process_workers 必须 >= 0")
    release = provider.release
    end = min(decision_at.date(), release.end_date)
    instruments = _scoped_instruments(release, symbols)
    total = len(instruments)
    if total == 0:
        raise FactorAnalysisError(
            "冻结发布没有可计算的标的"
            if not symbols
            else "explicit_symbols 声明的标的均不在冻结发布 instruments 中"
        )
    if process_workers > 0:
        return await _build_price_feature_snapshot_in_processes(
            provider=provider,
            decision_at=decision_at,
            code_version=code_version,
            momentum_lookback=momentum_lookback,
            volatility_windows=volatility_windows,
            process_workers=process_workers,
            on_progress=on_progress,
            symbols=symbols,
            external_executor=process_executor,
        )

    queue: asyncio.Queue[ReleasedInstrument] = asyncio.Queue()
    for instrument in instruments:
        queue.put_nowait(instrument)
    observations_by_symbol: dict[str, list[FeatureObservation]] = {}
    done_count = 0

    async def _worker() -> None:
        nonlocal done_count
        while True:
            try:
                instrument = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            columns = await provider.fetch_close_history(
                Symbol(instrument.code, instrument.market),
                release.period,
                release.start_date,
                end,
                decision_at=decision_at,
                adjust=release.adjustment,
            )
            # 特征计算(momentum / 波动率滚动窗口)是逐标的纯 CPU 段,经
            # to_thread 卸载(issue #286):全市场重算时不再阻塞事件循环;
            # IO(Parquet 解码)已由 provider 内部的 to_thread 承担。
            # issue #300:输入为列式 close(future 直出),不再逐行建对象。
            observations_by_symbol[instrument.code] = await asyncio.to_thread(
                _build_price_observations,
                source=release.source,
                source_version=release.version,
                symbol=instrument.code,
                market=instrument.market,
                asset_class=instrument.asset_class,
                columns=columns,
                momentum_lookback=momentum_lookback,
                volatility_windows=volatility_windows,
            )
            done_count += 1
            if on_progress is not None:
                on_progress(instrument.code, done_count, total)

    worker_count = min(max_concurrency, total)
    workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    observations = [
        observation
        for instrument in instruments
        for observation in observations_by_symbol[instrument.code]
    ]
    if not observations:
        raise FactorAnalysisError("冻结发布在决策时点没有足够数据计算价格特征")
    windows = {"momentum": momentum_lookback, "downside_volatility": 60}
    windows.update(
        {f"volatility_{window}d": window for window in volatility_windows}
    )
    return build_feature_snapshot(
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        decision_at=decision_at,
        code_version=code_version,
        observations=observations,
        calculation_windows={
            name: window
            for name, window in windows.items()
            if any(item.feature_name == name for item in observations)
        },
        transformations={
            item.feature_name: get_factor_definition(
                item.feature_name
            ).default_transform
            for item in observations
        },
        neutralization={
            item.feature_name: get_factor_definition(
                item.feature_name
            ).default_neutralization
            for item in observations
        },
    )


def _correlation(
    scores: dict[str, float],
    returns: dict[str, float],
    *,
    rank: bool,
    minimum: int,
) -> float | None:
    common = sorted(set(scores) & set(returns))
    if len(common) < minimum:
        return None
    x = np.asarray([scores[symbol] for symbol in common], dtype=np.float64)
    y = np.asarray([returns[symbol] for symbol in common], dtype=np.float64)
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < minimum:
        return None
    x = x[mask]
    y = y[mask]
    if rank:
        x = _average_rank(x)
        y = _average_rank(y)
    if float(np.std(x)) <= _EPS or float(np.std(y)) <= _EPS:
        return 0.0
    correlation = float(np.corrcoef(x, y)[0, 1])
    return correlation if math.isfinite(correlation) else 0.0


def _average_rank(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=np.float64)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and sorted_values[end] == sorted_values[start]:
            end += 1
        average = (start + 1 + end) / 2.0
        ranks[order[start:end]] = average
        start = end
    return ranks


def _quantile_members(
    scores: dict[str, float],
    returns: dict[str, float],
    *,
    quantiles: int,
    minimum: int,
) -> dict[int, tuple[str, ...]] | None:
    common = [
        symbol
        for symbol in sorted(set(scores) & set(returns))
        if math.isfinite(scores[symbol]) and math.isfinite(returns[symbol])
    ]
    if len(common) < max(minimum, quantiles):
        return None
    ranked = sorted(common, key=lambda symbol: (scores[symbol], symbol))
    indexes = np.array_split(np.asarray(ranked, dtype=object), quantiles)
    return {
        index + 1: tuple(str(symbol) for symbol in bucket.tolist())
        for index, bucket in enumerate(indexes)
    }


def _average_membership_turnover(members: list[set[str]]) -> float:
    if len(members) < 2:
        return 0.0
    turnover: list[float] = []
    for previous, current in pairwise(members):
        denominator = max(len(previous), len(current), 1)
        turnover.append(len(current - previous) / denominator)
    return float(np.mean(turnover))


def _analyse_neighbourhood(
    periods: list[FactorPeriod],
    neighbourhood: dict[str, list[dict[str, float]]],
    *,
    minimum: int,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for label, scores_history in sorted(neighbourhood.items()):
        if len(scores_history) != len(periods):
            raise FactorAnalysisError(
                f"参数邻域 {label} 期数与基础实验不一致"
            )
        values: list[float] = []
        for period, scores in zip(periods, scores_history, strict=True):
            value = _correlation(
                scores,
                period.forward_returns[1],
                rank=True,
                minimum=minimum,
            )
            if value is not None:
                values.append(value)
        if values:
            result[label] = float(np.mean(values))
    return result


def _analyse_regime(
    regime: str,
    periods: list[FactorPeriod],
    *,
    quantiles: int,
    minimum: int,
) -> RegimeAnalysis:
    ranks: list[float] = []
    pearsons: list[float] = []
    net_returns: list[float] = []
    for period in periods:
        returns = period.forward_returns[1]
        rank = _correlation(period.scores, returns, rank=True, minimum=minimum)
        pearson = _correlation(
            period.scores, returns, rank=False, minimum=minimum
        )
        if rank is not None:
            ranks.append(rank)
        if pearson is not None:
            pearsons.append(pearson)
        buckets = _quantile_members(
            period.scores,
            returns,
            quantiles=quantiles,
            minimum=minimum,
        )
        if buckets is None:
            continue
        top = buckets[quantiles]
        bottom = buckets[1]
        gross = _mean_or_zero([returns[symbol] for symbol in top])
        gross -= _mean_or_zero([returns[symbol] for symbol in bottom])
        costs = _mean_or_zero(
            [period.transaction_costs.get(symbol, 0.0) for symbol in top]
        )
        costs += _mean_or_zero(
            [period.transaction_costs.get(symbol, 0.0) for symbol in bottom]
        )
        net_returns.append(gross - costs)
    return RegimeAnalysis(
        regime=regime,
        rank_ic=_mean_or_zero(ranks),
        pearson_ic=_mean_or_zero(pearsons),
        net_long_short_return=_mean_or_zero(net_returns),
        periods=len(periods),
    )


def _risk_contributions(
    symbols: tuple[str, ...],
    covariance: npt.NDArray[np.float64],
    weights: dict[str, float],
) -> tuple[float, dict[str, float], dict[str, float]]:
    if not weights:
        zeros = dict.fromkeys(symbols, 0.0)
        return 0.0, zeros, dict(zeros)
    unknown = set(weights) - set(symbols)
    if unknown:
        raise RiskModelError(f"组合权重包含风险模型外标的: {sorted(unknown)}")
    if any(not math.isfinite(value) for value in weights.values()):
        raise RiskModelError("组合权重必须为有限数")
    vector = np.asarray([weights.get(symbol, 0.0) for symbol in symbols])
    variance = float(vector @ covariance @ vector)
    if variance <= _EPS:
        zeros = dict.fromkeys(symbols, 0.0)
        return 0.0, zeros, dict(zeros)
    daily_volatility = math.sqrt(variance)
    annualization = math.sqrt(_TRADING_DAYS)
    marginal = covariance @ vector / daily_volatility * annualization
    component = vector * marginal
    return (
        daily_volatility * annualization,
        {
            symbol: float(marginal[index])
            for index, symbol in enumerate(symbols)
        },
        {
            symbol: float(component[index])
            for index, symbol in enumerate(symbols)
        },
    )


def _mean_or_zero(values: list[float] | list[int]) -> float:
    return float(np.mean(values)) if values else 0.0


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区")


__all__ = [
    "PRICE_FEATURE_POOL_MAX_TASKS_PER_CHILD",
    "AlphaAnalysisReport",
    "BasicRiskModel",
    "CrossMarketSnapshot",
    "DecayPoint",
    "FactorAnalysisError",
    "FactorPeriod",
    "MarketInputError",
    "MarketInputObservation",
    "PriceFeatureProcessPool",
    "QuantileReturn",
    "RegimeAnalysis",
    "RiskExposure",
    "RiskModelError",
    "analyze_alpha_factor",
    "build_cross_market_snapshot",
    "build_cross_section_feature_snapshot",
    "build_price_feature_snapshot",
    "estimate_basic_risk_model",
]
