"""multi_factor 信号引擎:冻结输入 → ``NormalizedSignal``(issue #170)。

把 ``LoadedDecisionContext`` 的机械字段(特征快照 / 价格)+ 已发布
``ResearchStrategySpec`` 求值为 ``NormalizedSignal`` 元组:

* FeatureGraph 按拓扑序求值全部白名单算子(identity/sma/ema/return/
  volatility/zscore/cross_section_rank/winsorize/negate/subtract/ratio/
  weighted_sum);
* SignalRules 求值全部 comparator(gt/gte/lt/lte/between/cross_above/
  cross_below/rank_top/rank_bottom),并按 conflict_policy / default_action
  消解冲突;
* 信号标的必须是 included 候选子集(组合流水线 ``PortfolioPipelineAdapter``
  校验)。

求值模型:每个节点产出「值序列」``dict[symbol, list[float]]``(时间升序)。
时序算子(sma/ema/return/volatility/zscore)基于决策日前价格序列逐点滚动、
输出全序列;横截面算子(rank/negate/winsorize/subtract/ratio/weighted_sum)
取输入序列最后值(即决策时点横截面)求值、输出单点序列;identity 对因子源
输出特征快照单点值、对 ``close`` 输出价格序列。信号规则作用于最终值,
cross_* 规则比较两个节点的完整序列。

边界:纯离线研究域 —— 不连接 broker / 不发送订单 / 不修改持仓;只读冻结
发布与因子快照,产出组合流水线输入。
"""

from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from finboard_backtest.factors.combine import (
    CombinationConfig,
    CombinationMethod,
    FactorWeight,
    combine_scores,
)
from finboard_backtest.factors.standardize import (
    apply_direction,
    rank_normalize,
    winsorize,
)
from finboard_backtest.research_run.adapters import ResearchStrategyAdapter
from finboard_backtest.research_run.contracts import (
    REBALANCE_FREQUENCIES,
    DecisionBundle,
    EquityPoint,
    FeatureValue,
    NormalizedSignal,
    ResearchExecutionMode,
    ResearchRunManifest,
    ResearchRunReport,
    UniverseCandidate,
    execution_mode_for,
)
from finboard_backtest.research_run.frozen_loader import (
    FeatureSnapshotProvider,
    FrozenInputLoader,
    LoadedDecisionContext,
    ReleaseProviderFactory,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.strategy_spec.contracts import (
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
)
from finboard_backtest.strategy_spec.universe import (
    UniverseCandidate as SpecUniverseCandidate,
)
from finboard_backtest.strategy_spec.universe import explain_universe
from finboard_backtest.strategy_spec.universe_precheck import (
    resolvable_feature_names,
    universe_filter_warnings,
)

if TYPE_CHECKING:
    from finboard_backtest.portfolio import CovarianceEstimate
    from finboard_data.releases import FrozenReleaseProvider

logger = structlog.get_logger(__name__)

#: 信号引擎当前支持的策略类型(其余 kind 继续明确报 not_implemented)。
SIGNAL_ENGINE_STRATEGY_KINDS: frozenset[str] = frozenset({"multi_factor"})

#: 节点值序列:symbol → 时间升序数值序列(单点序列长度 1)。
NodeSeries = dict[str, list[float]]
#: 节点最终横截面值:symbol → 标量。
NodeValue = dict[str, float]


# ---------------------------------------------------------------------------
# 特征重组
# ---------------------------------------------------------------------------


def _features_by_source(
    features: Sequence[FeatureValue],
) -> dict[str, dict[str, float]]:
    """``FeatureValue`` 列表重组为 {feature_id: {symbol: value}}。

    同一 (symbol, feature_id) 多条观测(多快照)时取 ``available_at`` 最新一条,
    保证决策日语义确定。
    """
    best: dict[tuple[str, str], FeatureValue] = {}
    for feature in features:
        key = (feature.symbol, feature.feature_id)
        current = best.get(key)
        if current is None or feature.available_at > current.available_at:
            best[key] = feature
    out: dict[str, dict[str, float]] = {}
    for (symbol, feature_id), item in best.items():
        value = item.value
        if value is None:
            continue
        out.setdefault(feature_id, {})[symbol] = value
    return out


# ---------------------------------------------------------------------------
# FeatureGraph 求值
# ---------------------------------------------------------------------------


def _topological_order(nodes: Sequence[FeatureNode]) -> list[FeatureNode]:
    """Kahn 拓扑排序(contracts 已校验无环,这里保证确定性输出)。"""
    by_id = {node.node_id: node for node in nodes}
    indegree = {node.node_id: len(node.inputs) for node in nodes}
    ready = [node.node_id for node in nodes if not node.inputs]
    ordered: list[str] = []
    while ready:
        ready.sort()
        node_id = ready.pop(0)
        ordered.append(node_id)
        for node in nodes:
            if node_id in node.inputs:
                indegree[node.node_id] -= 1
                if indegree[node.node_id] == 0:
                    ready.append(node.node_id)
    if len(ordered) != len(nodes):
        raise ValueError("FeatureGraph 存在循环依赖")
    return [by_id[node_id] for node_id in ordered]


def _last_values(series: NodeSeries) -> NodeValue:
    return {symbol: values[-1] for symbol, values in series.items() if values}


def _single_point(values: NodeValue) -> NodeSeries:
    return {symbol: [value] for symbol, value in values.items()}


def _rolling_mean(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    window = min(window, len(values))
    out: list[float] = []
    for index in range(len(values)):
        lo = max(0, index - window + 1)
        out.append(sum(values[lo : index + 1]) / (index - lo + 1))
    return out


def _ema(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (window + 1)
    out: list[float] = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _rolling_returns(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    out: list[float] = []
    for index in range(1, len(values)):
        base_index = max(0, index - window)
        base = values[base_index]
        if base == 0 or not math.isfinite(base):
            out.append(float("nan"))
        else:
            out.append(values[index] / base - 1)
    return out


def _rolling_volatility(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    returns = _rolling_returns(values, window)
    out: list[float] = []
    for index in range(len(values)):
        lo = max(0, index - window + 1)
        window_returns = [item for item in returns[lo:index] if math.isfinite(item)]
        if len(window_returns) < 2:
            out.append(float("nan"))
            continue
        mean = sum(window_returns) / len(window_returns)
        variance = sum((item - mean) ** 2 for item in window_returns) / (len(window_returns) - 1)
        out.append(math.sqrt(variance))
    return out


def _rolling_zscore(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    out: list[float] = []
    for index in range(len(values)):
        lo = max(0, index - window + 1)
        window_values = values[lo : index + 1]
        if len(window_values) < 2:
            out.append(0.0)
            continue
        mean = sum(window_values) / len(window_values)
        variance = sum((item - mean) ** 2 for item in window_values) / len(window_values)
        if variance <= 1e-12:
            out.append(0.0)
        else:
            out.append((values[index] - mean) / math.sqrt(variance))
    return out


def _evaluate_node(
    node: FeatureNode,
    *,
    inputs: Mapping[str, NodeSeries],
    features_by_source: Mapping[str, Mapping[str, float]],
    prices: Mapping[str, float],
    price_series: Mapping[str, Sequence[float]],
) -> NodeSeries:
    operator = node.operator
    if operator is FeatureOperator.IDENTITY:
        assert node.source is not None
        if node.source in features_by_source:
            return _single_point(dict(features_by_source[node.source]))
        if node.source == "close" and price_series:
            return {symbol: list(values) for symbol, values in price_series.items()}
        if node.source == "close" and prices:
            return _single_point(dict(prices))
        raise ValueError(f"identity 节点缺少数据源: {node.source}")

    if (
        operator
        in {
            FeatureOperator.SIMPLE_MOVING_AVERAGE,
            FeatureOperator.EXPONENTIAL_MOVING_AVERAGE,
            FeatureOperator.RETURN,
            FeatureOperator.VOLATILITY,
            FeatureOperator.ZSCORE,
        }
        and node.window is not None
    ):
        window = node.window
        input_series = inputs[node.inputs[0]]
        transform: Callable[[list[float]], list[float]]
        if operator is FeatureOperator.SIMPLE_MOVING_AVERAGE:
            transform = lambda values: _rolling_mean(values, window)  # noqa: E731
        elif operator is FeatureOperator.EXPONENTIAL_MOVING_AVERAGE:
            transform = lambda values: _ema(values, window)  # noqa: E731
        elif operator is FeatureOperator.RETURN:
            transform = lambda values: _rolling_returns(values, window)  # noqa: E731
        elif operator is FeatureOperator.VOLATILITY:
            transform = lambda values: _rolling_volatility(values, window)  # noqa: E731
        else:
            transform = lambda values: _rolling_zscore(values, window)  # noqa: E731
        return {symbol: transform(list(values)) for symbol, values in input_series.items()}

    final_inputs = {name: _last_values(series) for name, series in inputs.items()}
    if operator is FeatureOperator.CROSS_SECTION_RANK:
        return _single_point(rank_normalize(final_inputs[node.inputs[0]]))
    if operator is FeatureOperator.NEGATE:
        return _single_point(apply_direction(final_inputs[node.inputs[0]], -1.0))
    if operator is FeatureOperator.WINSORIZE:
        assert node.lower_percentile is not None
        assert node.upper_percentile is not None
        return _single_point(
            winsorize(
                final_inputs[node.inputs[0]],
                lower_pct=node.lower_percentile,
                upper_pct=node.upper_percentile,
            )
        )
    if operator is FeatureOperator.SUBTRACT:
        left, right = final_inputs[node.inputs[0]], final_inputs[node.inputs[1]]
        symbols = sorted(set(left) | set(right))
        return _single_point(
            {
                symbol: left.get(symbol, float("nan")) - right.get(symbol, float("nan"))
                for symbol in symbols
            }
        )
    if operator is FeatureOperator.RATIO:
        left, right = final_inputs[node.inputs[0]], final_inputs[node.inputs[1]]
        symbols = sorted(set(left) | set(right))
        out: NodeValue = {}
        for symbol in symbols:
            denominator = right.get(symbol)
            out[symbol] = (
                left.get(symbol, float("nan")) / denominator
                if denominator not in (None, 0) and math.isfinite(denominator)
                else float("nan")
            )
        return _single_point(out)
    if operator is FeatureOperator.WEIGHTED_SUM:
        weights = tuple(
            FactorWeight(factor_name=name, weight=weight)
            for name, weight in zip(node.inputs, node.weights, strict=True)
        )
        config = CombinationConfig(weights=weights, method=CombinationMethod.WEIGHTED_AVERAGE)
        return _single_point(combine_scores(final_inputs, config))
    raise ValueError(f"未实现的 FeatureOperator: {operator.value}")


def evaluate_feature_graph(
    spec: ResearchStrategySpec,
    *,
    features_by_source: Mapping[str, Mapping[str, float]],
    prices: Mapping[str, float],
    price_series: Mapping[str, Sequence[float]] | None = None,
) -> tuple[dict[str, NodeSeries], dict[str, NodeValue]]:
    """按拓扑序求值全部特征节点。

    返回 ``(node_series, node_finals)``:前者保留每个节点的完整序列(供
    cross_* 规则使用),后者是每个节点输出的最终横截面值。
    """
    values: dict[str, NodeSeries] = {}
    for node in _topological_order(spec.feature_graph.nodes):
        inputs = {name: values[name] for name in node.inputs}
        values[node.node_id] = _evaluate_node(
            node,
            inputs=inputs,
            features_by_source=features_by_source,
            prices=prices,
            price_series=price_series or {},
        )
    finals = {node_id: _last_values(series) for node_id, series in values.items()}
    return values, finals


# ---------------------------------------------------------------------------
# SignalRules 求值
# ---------------------------------------------------------------------------


def _rank_ratios(values: NodeValue) -> dict[str, float]:
    """横截面排名比例:symbol → (rank-1)/n,0 为最高分、1 为最低分。"""
    pairs = sorted(
        ((value, symbol) for symbol, value in values.items() if math.isfinite(value)),
        reverse=True,
    )
    n = len(pairs)
    if n == 0:
        return {}
    ratios: dict[str, float] = {}
    for index, (_, symbol) in enumerate(pairs):
        ratios[symbol] = index / n
    return ratios


def _rule_matches(
    rule: SignalRule,
    *,
    symbol: str,
    value: float,
    finals: Mapping[str, NodeValue],
    series: Mapping[str, NodeSeries],
) -> bool:
    comparator = rule.comparator
    if comparator is SignalComparator.GREATER_THAN:
        assert rule.threshold is not None
        return value > rule.threshold
    if comparator is SignalComparator.GREATER_THAN_OR_EQUAL:
        assert rule.threshold is not None
        return value >= rule.threshold
    if comparator is SignalComparator.LESS_THAN:
        assert rule.threshold is not None
        return value < rule.threshold
    if comparator is SignalComparator.LESS_THAN_OR_EQUAL:
        assert rule.threshold is not None
        return value <= rule.threshold
    if comparator is SignalComparator.BETWEEN:
        assert rule.lower_bound is not None
        assert rule.upper_bound is not None
        return rule.lower_bound <= value <= rule.upper_bound
    if comparator in {SignalComparator.RANK_TOP, SignalComparator.RANK_BOTTOM}:
        assert rule.threshold is not None
        ratio = _rank_ratios(finals[rule.feature_id]).get(symbol)
        if ratio is None:
            return False
        if comparator is SignalComparator.RANK_TOP:
            return ratio < rule.threshold
        return ratio >= 1.0 - rule.threshold
    if comparator in {SignalComparator.CROSS_ABOVE, SignalComparator.CROSS_BELOW}:
        assert rule.reference_feature_id is not None
        first = series.get(rule.feature_id, {}).get(symbol)
        second = series.get(rule.reference_feature_id, {}).get(symbol)
        if not first or not second or len(first) < 2 or len(second) < 2:
            return False
        if comparator is SignalComparator.CROSS_ABOVE:
            return first[-2] <= second[-2] and first[-1] > second[-1]
        return first[-2] >= second[-2] and first[-1] < second[-1]
    raise ValueError(f"未实现的 SignalComparator: {comparator.value}")


def evaluate_signal_rules(
    spec: ResearchStrategySpec,
    *,
    node_finals: Mapping[str, NodeValue],
    node_series: Mapping[str, NodeSeries],
    included_symbols: frozenset[str],
    factor_snapshot_id: str | None,
) -> tuple[NormalizedSignal, ...]:
    """求值信号规则并消解冲突,产出 ``NormalizedSignal`` 元组。

    * ``highest_priority``:同 conflict_group 内保留 priority 最高的一条规则
      (同 priority 取规则定义顺序靠前者);
    * ``neutralize``:同组同时命中 buy 与 sell 时整组中性化(不产出);
    * ``default_action``:未命中任何规则的标的按默认动作产出(neutral 不产出,
      buy/sell 产出 0 分信号,由组合层中性过滤)。
    """
    rules = spec.signal_rules.rules
    policy = spec.signal_rules.conflict_policy
    default_action = spec.signal_rules.default_action

    hits: dict[str, list[tuple[SignalRule, float]]] = {}
    for symbol in sorted(included_symbols):
        for rule in rules:
            value = node_finals.get(rule.feature_id, {}).get(symbol)
            if value is None or not math.isfinite(value):
                continue
            if _rule_matches(
                rule,
                symbol=symbol,
                value=value,
                finals=node_finals,
                series=node_series,
            ):
                hits.setdefault(symbol, []).append((rule, value))

    resolved: dict[str, list[tuple[SignalRule, float]]] = {}
    for symbol, items in hits.items():
        groups: dict[str, list[tuple[SignalRule, float]]] = {}
        for item in items:
            groups.setdefault(item[0].conflict_group, []).append(item)
        chosen: list[tuple[SignalRule, float]] = []
        for _group, group_items in groups.items():
            if len(group_items) <= 1:
                chosen.extend(group_items)
                continue
            if policy is SignalConflictPolicy.HIGHEST_PRIORITY:
                best_priority = max(item[0].priority for item in group_items)
                chosen.append(
                    next(item for item in group_items if item[0].priority == best_priority)
                )
            else:  # NEUTRALIZE
                actions = {item[0].action for item in group_items}
                if SignalAction.BUY in actions and SignalAction.SELL in actions:
                    continue
                chosen.extend(group_items)
        resolved[symbol] = chosen

    signals: list[NormalizedSignal] = []
    for symbol in sorted(resolved):
        for rule, value in resolved[symbol]:
            if rule.action is SignalAction.NEUTRAL:
                continue
            # BUY 恒为正分、SELL 恒为负分(方向语义由动作决定,与特征值符号无关)。
            score = abs(value) if rule.action is SignalAction.BUY else -abs(value)
            signals.append(
                NormalizedSignal(
                    symbol=symbol,
                    score=score,
                    action=rule.action.value,
                    rule_id=rule.rule_id,
                    factor_snapshot_id=factor_snapshot_id,
                    rationale=rule.rationale,
                )
            )
    if default_action is not SignalAction.NEUTRAL:
        missing = sorted(symbol for symbol in included_symbols if symbol not in resolved)
        signals.extend(
            NormalizedSignal(
                symbol=symbol,
                score=0.0,
                action=default_action.value,
                rule_id="default_action",
                factor_snapshot_id=factor_snapshot_id,
                rationale="信号规则未命中,按默认动作输出中性信号",
            )
            for symbol in missing
        )
    return tuple(signals)


def build_normalized_signals(
    spec: ResearchStrategySpec,
    *,
    features: Sequence[FeatureValue],
    prices: Mapping[str, float],
    included_symbols: frozenset[str],
    factor_snapshot_id: str | None,
    price_series: Mapping[str, Sequence[float]] | None = None,
) -> tuple[NormalizedSignal, ...]:
    """单个决策时点的完整信号求值(特征重组 → 特征图 → 规则)。"""
    node_series, node_finals = evaluate_feature_graph(
        spec,
        features_by_source=_features_by_source(features),
        prices=prices,
        price_series=price_series,
    )
    return evaluate_signal_rules(
        spec,
        node_finals=node_finals,
        node_series=node_series,
        included_symbols=included_symbols,
        factor_snapshot_id=factor_snapshot_id,
    )


# ---------------------------------------------------------------------------
# 决策输入组装(真实适配器工厂)
# ---------------------------------------------------------------------------


async def _load_price_series(
    provider: FrozenReleaseProvider,
    symbols: Sequence[str],
    as_of: datetime,
) -> dict[str, list[float]]:
    """PIT 门控读取各标的决策时点前可见的完整 close 序列(时间升序)。"""
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    series: dict[str, list[float]] = {}
    for code in symbols:
        market = _market_from_value(code)
        bars = await provider.fetch_point_in_time_bars(
            Symbol(code=code, market=market),
            provider.release.period,
            provider.release.start_date,
            as_of.date(),
            decision_at=as_of,
            adjust=provider.release.adjustment,
        )
        if bars:
            series[code] = [float(bar.bar.close) for bar in bars]
    return series


async def _load_benchmark_curve(
    manifest: ResearchRunManifest,
    release_provider_factory: ReleaseProviderFactory,
) -> tuple[tuple[date, Decimal], ...]:
    """按 ``benchmark_config.symbol`` 从冻结发布取基准 bars,构建买入持有曲线。

    (issue #184)基准收益必须来自真实行情,不再依赖手工 ``overrides.return``。
    依次尝试 manifest 的每个数据发布,第一个能提供该标的 bars 的发布胜出
    (PIT 门按发布末日 17:00 放行全部日线);全部缺失返回空曲线,由
    ``build_report`` 落 null + 具名 warning。纯离线研究域,只读冻结发布。
    """
    from finboard_backtest.metrics import buy_and_hold_return
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    configured = manifest.benchmark_config.get("symbol")
    if not isinstance(configured, str) or not configured:
        return ()
    symbol = Symbol(code=configured, market=_market_from_value(configured))
    for release_ref in manifest.dataset_releases:
        try:
            provider = release_provider_factory(release_ref.artifact_id)
            bars = await provider.fetch_point_in_time_bars(
                symbol,
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                decision_at=datetime.combine(
                    provider.release.end_date,
                    time(hour=17, tzinfo=ZoneInfo("Asia/Shanghai")),
                ),
                adjust=provider.release.adjustment,
            )
        except Exception:
            logger.warning(
                "research_run.benchmark_release_unavailable",
                symbol=configured,
                release=release_ref.artifact_id,
            )
            continue
        if bars:
            return tuple(
                buy_and_hold_return(
                    [(item.bar.timestamp.date(), item.bar.close) for item in bars],
                    manifest.initial_capital,
                )
            )
    return ()


async def _release_trading_days(provider: FrozenReleaseProvider) -> list[date]:
    """读取发布交易日历(首个 ready 标的的已发布 bars,升序去重)。

    交易日历是公开知识,用非 PIT 的 ``fetch_bars`` 读取发布全范围;决策与
    成交发生在发布日期之后,不构成未来函数。
    """
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    for instrument in provider.release.instruments:
        if not instrument.ready:
            continue
        bars = await provider.fetch_bars(
            Symbol(
                code=instrument.code,
                market=_market_from_value(instrument.code),
            ),
            provider.release.period,
            provider.release.start_date,
            provider.release.end_date,
            adjust=provider.release.adjustment,
        )
        if bars:
            return sorted({bar.timestamp.date() for bar in bars})
    return []


async def _next_execution_at(
    provider: FrozenReleaseProvider,
    decision_at: datetime,
) -> datetime:
    """推断决策时点之后最近的交易日(发布交易日历,超限 fail-closed)。"""
    calendar = await _release_trading_days(provider)
    if not calendar:
        raise ValueError("发布无可用行情,无法推断成交交易日")
    for day in calendar:
        if day > decision_at.date():
            return datetime.combine(day, time(15, 0), tzinfo=decision_at.tzinfo)
    raise ValueError(
        f"决策时点 {decision_at.date().isoformat()} 之后无可用成交日"
        f"(发布交易日历止于 {calendar[-1].isoformat()})"
    )


def _estimate_covariance(
    price_series: Mapping[str, Sequence[float]],
) -> CovarianceEstimate | None:
    """从决策日前价格序列估计样本协方差(日收益率尺度)。

    全部标的取对齐后的共同窗口;观测不足 2 期时返回 ``None``,由组合流水线按
    fail_closed 拒绝(issue #91 语义:风险贡献硬约束启用时缺协方差必须失败)。
    """
    import numpy as np

    symbols = sorted(symbol for symbol, values in price_series.items() if len(values) >= 2)
    if len(symbols) < 2:
        return None
    n_obs = min(len(price_series[symbol]) for symbol in symbols) - 1
    if n_obs < 2:
        return None
    from finboard_backtest.portfolio import CovarianceEstimate as CovarianceEstimate

    returns: list[list[float]] = []
    for symbol in symbols:
        values = price_series[symbol][-(n_obs + 1) :]
        series_returns = [
            values[index] / values[index - 1] - 1
            for index in range(1, len(values))
            if values[index - 1] != 0
        ]
        returns.append(series_returns)
    matrix = np.cov(np.asarray(returns, dtype=np.float64))
    return CovarianceEstimate(
        matrix=matrix,
        tickers=symbols,
        shrinkage=0.0,
        n_observations=n_obs,
        method="sample",
    )


def _rebalance_frequency(manifest: ResearchRunManifest) -> str | None:
    """读取 ``parameters.rebalance_frequency``,非法值 fail-closed。"""
    value = manifest.parameters.get("rebalance_frequency")
    if value is None:
        return None
    if not isinstance(value, str) or value not in REBALANCE_FREQUENCIES:
        raise ValueError(f"rebalance_frequency 仅支持 {sorted(REBALANCE_FREQUENCIES)}: {value!r}")
    return value


def _period_bucket(day: date, frequency: str) -> tuple[int, int]:
    """交易日所属周期桶:(year, month) 或 (year, quarter)。"""
    if frequency == "monthly":
        return day.year, day.month
    if frequency == "quarterly":
        return day.year, (day.month - 1) // 3 + 1
    raise ValueError(f"未知调仓频率: {frequency}")


async def _derive_rebalance_decision_days(
    provider: FrozenReleaseProvider,
    frequency: str,
) -> list[tuple[datetime, str | None]]:
    """从发布交易日历推导多期回放的决策时点。

    每期(月 / 季)取该期最后一个交易日收盘后 15:00 决策,``factor_snapshot_id``
    恒为 ``None``(决策日与 features 由管线按冻结发布重算,不绑定单一快照)。
    成交发生在决策后的下一交易日,因此发布末尾没有后续交易日的期末不产生
    决策(该期无法成交,fail-closed 语义下直接排除)。
    """
    calendar = await _release_trading_days(provider)
    if not calendar:
        raise ValueError("发布无可用行情,无法推导多期决策时点")
    period_ends: dict[tuple[int, int], date] = {}
    for day in calendar:
        period_ends[_period_bucket(day, frequency)] = day
    decisions: list[tuple[datetime, str | None]] = []
    for day in sorted(period_ends.values()):
        # 期末之后必须存在下一交易日才能执行成交。
        if any(item > day for item in calendar):
            decisions.append((datetime.combine(day, time(15, 0), tzinfo=UTC), None))
    return decisions


async def _compute_period_features(
    provider: FrozenReleaseProvider,
    manifest: ResearchRunManifest,
    decision_at: datetime,
    release_id: str,
) -> tuple[FeatureValue, ...]:
    """按单个决策时点从冻结发布重算价格特征(issue #183)。

    复用 ``build_price_feature_snapshot``(与 feature_snapshot 任务同一实现,
    避免双源漂移):PIT 门控读取各标的收盘价,计算 momentum / volatility_Nd /
    downside_volatility,并把观测映射为 ``FeatureValue``(来源绑定冻结 release)。
    冻结因子快照里的基本面特征(如 pb)由 ``FrozenInputLoader`` 另行 PIT 加载,
    两者在 ``build_decision_inputs`` 合并。
    """
    from finboard_backtest.factor_lab import FactorAnalysisError, build_price_feature_snapshot

    try:
        snapshot = await build_price_feature_snapshot(
            provider=provider,
            decision_at=decision_at,
            code_version=manifest.code_version,
        )
    except FactorAnalysisError as exc:
        raise ValueError(
            f"决策时点 {decision_at.date().isoformat()} 无法从发布重算价格特征"
            f"(通常发布起点历史不足): {exc}"
        ) from exc
    return tuple(
        FeatureValue(
            symbol=observation.symbol,
            feature_id=observation.feature_name,
            value=observation.value,
            source_artifact_ids=(release_id,),
            available_at=observation.available_at,
        )
        for observation in snapshot.observations
    )


async def _market_close_map(
    provider: FrozenReleaseProvider,
    symbols: Sequence[str],
) -> dict[str, dict[date, Decimal]]:
    """读取各标的全区间收盘价映射(非 PIT;收盘价在当日收盘即公开)。"""
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    closes_by_symbol: dict[str, dict[date, Decimal]] = {}
    for code in symbols:
        bars = await provider.fetch_bars(
            Symbol(code=code, market=_market_from_value(code)),
            provider.release.period,
            provider.release.start_date,
            provider.release.end_date,
            adjust=provider.release.adjustment,
        )
        closes_by_symbol[code] = {bar.timestamp.date(): bar.close for bar in bars}
    return closes_by_symbol


async def build_daily_equity_curve(
    provider: FrozenReleaseProvider,
    manifest: ResearchRunManifest,
    decisions: Sequence[DecisionBundle],
) -> tuple[EquityPoint, ...]:
    """决策间每日 mark-to-market 权益曲线(issue #183)。

    以每次成交的执行日为账本切换边界,执行日之间只随行情波动:
    ``equity(d) = cash + Σ qty_i * close_i(d) * multiplier_i``;未建仓阶段
    权益 = 初始资金。覆盖发布交易日历的全部交易日;期末持仓按最后行情持续
    计值(纯回放不强制平仓)。
    边界:纯离线研究域,只读冻结发布,不连 broker / 不下单。
    """
    from bisect import bisect_right

    calendar = await _release_trading_days(provider)
    if not calendar:
        return ()
    segments: list[tuple[date, Decimal, dict[str, tuple[Decimal, Decimal]]]] = []
    for decision in decisions:
        if not decision.fills:
            continue
        execution_date = decision.fills[0].filled_at.date()
        positions: dict[str, tuple[Decimal, Decimal]] = {}
        for position in decision.positions:
            if position.quantity <= 0 or position.market_price <= 0:
                continue
            multiplier = position.market_value / (position.quantity * position.market_price)
            positions[position.symbol] = (position.quantity, multiplier)
        segments.append((execution_date, decision.ledger.cash, positions))
    segments.sort(key=lambda item: item[0])

    held_symbols = sorted({symbol for _, _, positions in segments for symbol in positions})
    closes_by_symbol = await _market_close_map(provider, held_symbols)
    # 每标的预排好日期键,停牌日用最近历史收盘承载。
    sorted_days: dict[str, list[date]] = {
        symbol: sorted(days) for symbol, days in closes_by_symbol.items()
    }

    points: list[EquityPoint] = []
    segment_index = 0
    for day in calendar:
        while segment_index + 1 < len(segments) and segments[segment_index + 1][0] <= day:
            segment_index += 1
        if not segments or segments[segment_index][0] > day:
            points.append(EquityPoint(trade_date=day, equity=manifest.initial_capital))
            continue
        cash, positions = segments[segment_index][1], segments[segment_index][2]
        equity = cash
        for symbol, (quantity, multiplier) in positions.items():
            days = sorted_days.get(symbol)
            if not days:
                continue
            index = bisect_right(days, day) - 1
            if index < 0:
                continue
            equity += quantity * closes_by_symbol[symbol][days[index]] * multiplier
        points.append(EquityPoint(trade_date=day, equity=equity))
    return tuple(points)


def _spec_universe_candidates(
    provider: FrozenReleaseProvider,
    context: LoadedDecisionContext,
    features_by_source: Mapping[str, Mapping[str, float]],
) -> tuple[SpecUniverseCandidate, ...]:
    """把发布标的映射为 ``UniverseSpec`` 候选(近似字段见函数体)。

    字段近似:listing_days 来自 list_date;price 来自决策日 close;停牌 / ST
    暂按发布快照静态近似(suspended_sessions / delist_date),待数据完备后细化。
    """
    candidates: list[SpecUniverseCandidate] = []
    decision_date = context.business_date
    for instrument in provider.release.instruments:
        if not instrument.ready:
            continue
        fields: dict[str, float | str | bool | None] = {
            name: by_symbol.get(instrument.code) for name, by_symbol in features_by_source.items()
        }
        price = context.prices.get(instrument.code)
        listing_days = 0
        if instrument.list_date is not None:
            listing_days = max(0, (decision_date - instrument.list_date).days)
        delisted = instrument.delist_date is not None and instrument.delist_date <= decision_date
        average_amount = fields.get("average_amount")
        candidates.append(
            SpecUniverseCandidate(
                symbol=instrument.code,
                market=instrument.market,
                asset_class=instrument.asset_class,
                listing_days=listing_days,
                average_amount=(
                    float(average_amount) if isinstance(average_amount, (int, float)) else None
                ),
                price=price,
                suspended=instrument.suspended_sessions > 0,
                delisted=delisted,
                is_st=False,
                data_completeness=float(instrument.coverage_pct),
                fields=fields,
            )
        )
    return tuple(candidates)


def _apply_universe_filter(
    spec: ResearchStrategySpec,
    provider: FrozenReleaseProvider,
    context: LoadedDecisionContext,
    features_by_source: Mapping[str, Mapping[str, float]],
) -> tuple[UniverseCandidate, ...]:
    """``UniverseSpec`` 过滤候选池(ranking / selection_limit / 排除规则)。"""
    decisions = explain_universe(
        spec.universe, _spec_universe_candidates(provider, context, features_by_source)
    )
    by_symbol = {item.symbol: item for item in decisions}
    out: list[UniverseCandidate] = []
    for candidate in context.candidates:
        decision = by_symbol.get(candidate.symbol)
        if decision is None:
            continue
        out.append(
            UniverseCandidate(
                symbol=candidate.symbol,
                included=decision.included,
                reasons=decision.reasons,
                asset_class=candidate.asset_class,
                market=candidate.market,
            )
        )
    return tuple(out)


def _runtime_available_features(
    spec: ResearchStrategySpec,
    features_by_source: Mapping[str, Mapping[str, float]],
) -> frozenset[str]:
    """运行时特征可解析集合 = 标准价格特征 + 规格 features + 本次观测特征。"""
    return resolvable_feature_names(
        feature_graph_sources=[
            node.source for node in spec.feature_graph.nodes if node.source is not None
        ],
        snapshot_feature_names=tuple(features_by_source),
    )


def _emit_universe_degradation_warnings(
    spec: ResearchStrategySpec,
    provider: FrozenReleaseProvider,
    features_by_source: Mapping[str, Mapping[str, float]],
    decision_at: datetime,
) -> None:
    """逐条声明「该过滤因缺元数据/特征未生效」的运行时 warning(issue #186)。"""
    for warning in universe_filter_warnings(
        spec.universe,
        provider.release.instruments,
        available_features=_runtime_available_features(spec, features_by_source),
    ):
        logger.warning(
            "research_run.universe_filter_degraded",
            decision_date=decision_at.date().isoformat(),
            condition=warning.condition,
            field=warning.field,
            code=warning.code,
            message=warning.message,
        )


def _empty_pool_error_message(
    spec: ResearchStrategySpec,
    candidates: Sequence[UniverseCandidate],
    instruments: Sequence[object],
    features_by_source: Mapping[str, Mapping[str, float]],
    decision_at: datetime,
) -> str:
    """执行期空池错误的根因信息:排除统计 + 缺失字段名(issue #186)。"""
    reasons: Counter[str] = Counter()
    for candidate in candidates:
        reasons.update(candidate.reasons)
    stats = "、".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
    missing: set[str] = set()
    for reason in reasons:
        if reason.startswith(("missing_required_field:", "missing_ranking_field:")):
            missing.add(reason.split(":", 1)[1])
        elif reason == "missing_average_amount":
            missing.add("average_amount")
        elif reason == "listing_age_below_minimum" and any(
            getattr(item, "list_date", None) is None for item in instruments
        ):
            missing.add("list_date")
        elif reason == "delisted":
            missing.add("delist_date")
        elif reason == "st_security":
            missing.add("st_marker")
    missing_text = "、".join(sorted(missing)) if missing else "无"
    return (
        f"决策日 {decision_at.date().isoformat()} 候选池为空: "
        f"共 {len(candidates)} 个候选标的全部被过滤。"
        f"排除统计: {stats or '无'};缺失字段: {missing_text}。"
        "请检查发布 instrument 元数据(list_date 等)与冻结特征是否齐备,"
        "或放宽 universe 过滤条件。"
    )


async def build_decision_inputs(
    manifest: ResearchRunManifest,
    *,
    release_provider_factory: ReleaseProviderFactory,
    snapshot_provider: FeatureSnapshotProvider,
) -> tuple[PortfolioDecisionInput, ...]:
    """按执行模式组装全部 ``PortfolioDecisionInput``(issue #170 / #183)。

    * single_shot(默认):决策日序列 = manifest.factor_snapshots 的
      ``decision_at`` 排序去重,每个决策日由 ``FrozenInputLoader`` 加载机械
      字段 → 价格序列 → universe 过滤 → 信号引擎求值 → 组装输入;
    * multi_period:决策日由 ``parameters.rebalance_frequency``(monthly/
      quarterly)按冻结发布交易日历推导,每期由管线重算 price features →
      合并冻结快照 PIT 观测 → 同样的 universe 过滤 / 信号求值;
    * 信号只对「included 且决策 / 成交价格齐备」的标的产出(组合流水线要求
      信号标的必须有价格与执行元数据)。
    """
    if not manifest.dataset_releases:
        raise ValueError("manifest 必须冻结至少一个数据发布")
    release_ref = manifest.dataset_releases[0]
    provider = release_provider_factory(release_ref.artifact_id)
    loader = FrozenInputLoader(
        release_provider_factory=release_provider_factory,
        snapshot_provider=snapshot_provider,
    )
    frequency = _rebalance_frequency(manifest)

    if frequency is not None:
        decision_days = await _derive_rebalance_decision_days(provider, frequency)
    else:
        decision_days = await _snapshot_decision_days(manifest, snapshot_provider)
    if not decision_days:
        raise ValueError("manifest 未冻结因子快照且未设置 rebalance_frequency,无法推导决策时点")

    inputs: list[PortfolioDecisionInput] = []
    for decision_at, snapshot_id in decision_days:
        execution_at = await _next_execution_at(provider, decision_at)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        if frequency is not None:
            period_features = await _compute_period_features(
                provider, manifest, decision_at, release_ref.artifact_id
            )
            features = (*period_features, *context.features)
        else:
            features = context.features
        features_by_source = _features_by_source(features)
        candidates = _apply_universe_filter(
            manifest.strategy_spec, provider, context, features_by_source
        )
        included = frozenset(item.symbol for item in candidates if item.included)
        # issue #186:运行时降级 warning —— 元数据/特征缺失时声明该过滤未生效,
        # 对齐快照链路 `instrument_profiles_unavailable` 的语义(不静默)。
        _emit_universe_degradation_warnings(
            manifest.strategy_spec,
            provider,
            features_by_source,
            decision_at,
        )
        if not included:
            # issue #186:执行期空池错误附根因(缺失字段名 + 排除统计),
            # 不再只有 portfolio_pipeline 的泛化「候选池为空」。
            raise ValueError(
                _empty_pool_error_message(
                    manifest.strategy_spec,
                    candidates,
                    provider.release.instruments,
                    features_by_source,
                    decision_at,
                )
            )
        # 信号标的必须同时具备决策价、成交价、执行元数据与可估计收益的历史。
        signalable = (
            included
            & frozenset(context.prices)
            & frozenset(context.execution_prices)
            & frozenset(context.lot_info)
        )
        price_series = await _load_price_series(provider, tuple(signalable), decision_at)
        signalable = frozenset(
            symbol for symbol in signalable if len(price_series.get(symbol, ())) >= 2
        )
        signals = build_normalized_signals(
            manifest.strategy_spec,
            features=features,
            prices=context.prices,
            included_symbols=signalable,
            factor_snapshot_id=None if frequency is not None else snapshot_id,
            price_series=price_series,
        )
        inputs.append(
            PortfolioDecisionInput(
                business_date=context.business_date,
                decision_at=context.decision_at,
                execution_at=context.execution_at,
                candidates=candidates,
                features=features,
                signals=signals,
                prices=context.prices,
                execution_prices=context.execution_prices,
                lot_info=context.lot_info,
                input_artifact_ids=context.input_artifact_ids,
                covariance=_estimate_covariance(price_series),
            )
        )
    return tuple(inputs)


async def _snapshot_decision_days(
    manifest: ResearchRunManifest,
    snapshot_provider: FeatureSnapshotProvider,
) -> list[tuple[datetime, str | None]]:
    """按冻结因子快照的 ``decision_at`` 推导单时点决策序列(排序去重)。"""
    decision_days: list[tuple[datetime, str | None]] = []
    for ref in manifest.factor_snapshots:
        snapshot = await snapshot_provider(ref.artifact_id)
        if snapshot is None:
            raise ValueError(f"因子快照缺失: {ref.artifact_id}")
        decision_days.append((snapshot.decision_at, snapshot.snapshot_id))
    return sorted(set(decision_days))


class SignalEnginePipelineAdapter:
    """``multi_factor`` 规格的真实信号引擎适配器(惰性加载)。

    工厂同步构造本适配器,首个 ``decisions()`` 迭代时才异步加载冻结产物与
    信号 —— 加载失败会落在 ``ResearchRunCoordinator`` 的异常分流内,
    ``research_runs`` 状态正确迁移为 FAILED(issue #170 伴生缺陷 A)。

    * single_shot:决策日来自冻结因子快照,``execution_mode=single_shot``;
    * multi_period:``parameters.rebalance_frequency`` 按发布交易日历推导
      多期决策,每期重算 features,signal_engine 决策间按冻结行情每日
      mark-to-market 产出 ``equity_curve``(issue #183)。
    """

    def __init__(
        self,
        *,
        manifest: ResearchRunManifest,
        release_provider_factory: ReleaseProviderFactory,
        snapshot_provider: FeatureSnapshotProvider,
    ) -> None:
        if manifest.strategy_kind not in SIGNAL_ENGINE_STRATEGY_KINDS:
            raise ValueError(
                f"信号引擎当前仅支持 {sorted(SIGNAL_ENGINE_STRATEGY_KINDS)}"
                f" 规格: {manifest.strategy_kind}"
            )
        self.strategy_kind = manifest.strategy_kind
        self._manifest = manifest
        self._release_provider_factory = release_provider_factory
        self._snapshot_provider = snapshot_provider
        self._inputs: tuple[PortfolioDecisionInput, ...] | None = None
        self._equity_curve: tuple[EquityPoint, ...] = ()
        self._benchmark_curve: tuple[tuple[date, Decimal], ...] = ()

    @property
    def execution_mode(self) -> ResearchExecutionMode:
        return execution_mode_for(self._manifest.parameters)

    def validate_manifest(self, manifest: ResearchRunManifest) -> None:
        if manifest.strategy_kind != self.strategy_kind:
            raise ValueError(
                f"manifest={manifest.strategy_kind} 与 adapter={self.strategy_kind} 不一致"
            )
        delegate = PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=(),
            required_capabilities=(),
        )
        delegate.validate_manifest(manifest)

    async def _load(self) -> PortfolioPipelineAdapter:
        if self._inputs is None:
            self._inputs = await build_decision_inputs(
                self._manifest,
                release_provider_factory=self._release_provider_factory,
                snapshot_provider=self._snapshot_provider,
            )
        return PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._inputs,
        )

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        adapter = await self._load()
        collected: list[DecisionBundle] = []
        async for decision in adapter.decisions(manifest):
            yield decision
            collected.append(decision)
        # 多期回放:决策全部产出后按冻结行情构建每日权益曲线(离线圈内,
        # 只在 coordinator 完整消费决策后执行;中断时曲线保持为空)。
        if (
            # 这里用执行时 manifest 判定,避免 replica 的 manifest 与
            # 构造期 manifest 不一致(重放时参数不变,两者等价)。
            execution_mode_for(manifest.parameters) is ResearchExecutionMode.MULTI_PERIOD
            and collected
        ):
            release_ref = manifest.dataset_releases[0]
            provider = self._release_provider_factory(release_ref.artifact_id)
            self._equity_curve = await build_daily_equity_curve(provider, manifest, collected)
        # 基准曲线(issue #184):两种执行模式都按 benchmark_config.symbol
        # 从冻结发布取行情;缺失落空曲线,由 build_report 记 warning。
        self._benchmark_curve = await _load_benchmark_curve(
            manifest, self._release_provider_factory
        )

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionBundle],
    ) -> ResearchRunReport:
        delegate = PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._inputs or (),
        )
        return delegate.build_report(
            manifest,
            decisions,
            equity_curve=self._equity_curve,
            benchmark_curve=self._benchmark_curve,
        )


def build_signal_engine_adapter_factory(
    session_maker: async_sessionmaker[Any],
    *,
    release_root: str | Path | None = None,
) -> Callable[[ResearchRunManifest], ResearchStrategyAdapter]:
    """构造 CLI 可注入的 ``AdapterFactory``(multi_factor 分发)。

    延迟导入 persistence / data 依赖(finboard-backtest 不直接依赖
    finboard-persistence);非 multi_factor 规格继续明确报 not_implemented。
    """

    def _factory(manifest: ResearchRunManifest) -> ResearchStrategyAdapter:
        if manifest.strategy_kind not in SIGNAL_ENGINE_STRATEGY_KINDS:
            from finboard_backtest.background_jobs.contracts import ExecutorError

            raise ExecutorError(
                code="signal_engine_not_implemented",
                summary=(
                    f"信号引擎当前仅支持 {sorted(SIGNAL_ENGINE_STRATEGY_KINDS)}"
                    f" 规格;{manifest.strategy_kind} 尚未实现"
                ),
                retryable=False,
            )
        from finboard_data.releases import FrozenReleaseProvider
        from finboard_persistence import FeatureSnapshotRepository

        root = Path(
            release_root
            if release_root is not None
            else os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
        )

        def _release_factory(release_id: str) -> FrozenReleaseProvider:
            return FrozenReleaseProvider(release_root=root, release_id=release_id)

        async def _snapshot_provider(snapshot_id: str) -> object:
            async with session_maker() as session:
                return await FeatureSnapshotRepository(session).get(snapshot_id)

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=_release_factory,
            snapshot_provider=_snapshot_provider,  # type: ignore[arg-type]
        )

    return _factory


__all__ = [
    "SIGNAL_ENGINE_STRATEGY_KINDS",
    "SignalEnginePipelineAdapter",
    "build_daily_equity_curve",
    "build_decision_inputs",
    "build_normalized_signals",
    "build_signal_engine_adapter_factory",
    "evaluate_feature_graph",
    "evaluate_signal_rules",
]
