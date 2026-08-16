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
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    DecisionBundle,
    FeatureValue,
    NormalizedSignal,
    ResearchRunManifest,
    ResearchRunReport,
    UniverseCandidate,
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

if TYPE_CHECKING:
    from finboard_backtest.portfolio import CovarianceEstimate
    from finboard_data.releases import FrozenReleaseProvider

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
        variance = sum((item - mean) ** 2 for item in window_returns) / (
            len(window_returns) - 1
        )
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
        variance = sum((item - mean) ** 2 for item in window_values) / len(
            window_values
        )
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
                chosen.append(next(item for item in group_items if item[0].priority == best_priority))
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
        missing = sorted(
            symbol for symbol in included_symbols if symbol not in resolved
        )
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


async def _next_execution_at(
    provider: FrozenReleaseProvider,
    decision_at: datetime,
) -> datetime:
    """推断决策时点之后最近的交易日(发布交易日历,超限 fail-closed)。

    交易日历是公开知识,用非 PIT 的 ``fetch_bars`` 读取发布全范围 bars;
    执行发生在决策之后,不构成未来函数。
    """
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    calendar: list[date] | None = None
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
            calendar = sorted({bar.timestamp.date() for bar in bars})
            break
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

    symbols = sorted(
        symbol
        for symbol, values in price_series.items()
        if len(values) >= 2
    )
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
            name: by_symbol.get(instrument.code)
            for name, by_symbol in features_by_source.items()
        }
        price = context.prices.get(instrument.code)
        listing_days = 0
        if instrument.list_date is not None:
            listing_days = max(0, (decision_date - instrument.list_date).days)
        delisted = (
            instrument.delist_date is not None
            and instrument.delist_date <= decision_date
        )
        average_amount = fields.get("average_amount")
        candidates.append(
            SpecUniverseCandidate(
                symbol=instrument.code,
                market=instrument.market,
                asset_class=instrument.asset_class,
                listing_days=listing_days,
                average_amount=(
                    float(average_amount)
                    if isinstance(average_amount, (int, float))
                    else None
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


async def build_decision_inputs(
    manifest: ResearchRunManifest,
    *,
    release_provider_factory: ReleaseProviderFactory,
    snapshot_provider: FeatureSnapshotProvider,
) -> tuple[PortfolioDecisionInput, ...]:
    """按 validation 冻结的快照决策时点组装全部 ``PortfolioDecisionInput``。

    * 决策日序列 = manifest.factor_snapshots 的 ``decision_at`` 排序去重;
    * 每个决策日:``FrozenInputLoader`` 加载机械字段 → 价格序列 → universe
      过滤 → 信号引擎求值 → 组装输入;
    * 信号只对「included 且决策 / 成交价格齐备」的标的产出(组合流水线要求
      信号标的必须有价格与执行元数据)。
    """
    if not manifest.dataset_releases:
        raise ValueError("manifest 必须冻结至少一个数据发布")
    decision_days: list[tuple[datetime, str | None]] = []
    for ref in manifest.factor_snapshots:
        snapshot = await snapshot_provider(ref.artifact_id)
        if snapshot is None:
            raise ValueError(f"因子快照缺失: {ref.artifact_id}")
        decision_days.append((snapshot.decision_at, snapshot.snapshot_id))
    decision_days = sorted(set(decision_days))
    if not decision_days:
        raise ValueError("manifest 未冻结因子快照,无法推导决策时点")

    release_ref = manifest.dataset_releases[0]
    provider = release_provider_factory(release_ref.artifact_id)
    loader = FrozenInputLoader(
        release_provider_factory=release_provider_factory,
        snapshot_provider=snapshot_provider,
    )

    inputs: list[PortfolioDecisionInput] = []
    for decision_at, snapshot_id in decision_days:
        execution_at = await _next_execution_at(provider, decision_at)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        features_by_source = _features_by_source(context.features)
        candidates = _apply_universe_filter(
            manifest.strategy_spec, provider, context, features_by_source
        )
        included = frozenset(
            item.symbol for item in candidates if item.included
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
            symbol
            for symbol in signalable
            if len(price_series.get(symbol, ())) >= 2
        )
        signals = build_normalized_signals(
            manifest.strategy_spec,
            features=context.features,
            prices=context.prices,
            included_symbols=signalable,
            factor_snapshot_id=snapshot_id,
            price_series=price_series,
        )
        inputs.append(
            PortfolioDecisionInput(
                business_date=context.business_date,
                decision_at=context.decision_at,
                execution_at=context.execution_at,
                candidates=candidates,
                features=context.features,
                signals=signals,
                prices=context.prices,
                execution_prices=context.execution_prices,
                lot_info=context.lot_info,
                input_artifact_ids=context.input_artifact_ids,
                covariance=_estimate_covariance(price_series),
            )
        )
    return tuple(inputs)


class SignalEnginePipelineAdapter:
    """``multi_factor`` 规格的真实信号引擎适配器(惰性加载)。

    工厂同步构造本适配器,首个 ``decisions()`` 迭代时才异步加载冻结产物与
    信号 —— 加载失败会落在 ``ResearchRunCoordinator`` 的异常分流内,
    ``research_runs`` 状态正确迁移为 FAILED(issue #170 伴生缺陷 A)。
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
        async for decision in adapter.decisions(manifest):
            yield decision

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionBundle],
    ) -> ResearchRunReport:
        delegate = PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._inputs or (),
        )
        return delegate.build_report(manifest, decisions)


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
    "build_decision_inputs",
    "build_normalized_signals",
    "build_signal_engine_adapter_factory",
    "evaluate_feature_graph",
    "evaluate_signal_rules",
]
