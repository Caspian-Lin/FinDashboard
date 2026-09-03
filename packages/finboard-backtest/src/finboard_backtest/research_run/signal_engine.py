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

import asyncio
import contextlib
import math
import os
import weakref
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
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
    FrozenArtifactRef,
    JsonValue,
    NormalizedSignal,
    ResearchExecutionMode,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunReport,
    UniverseCandidate,
    execution_mode_for,
)
from finboard_backtest.research_run.failure_context import (
    attach_decision_load_context,
)
from finboard_backtest.research_run.frozen_loader import (
    FeatureSnapshotProvider,
    FrozenInputLoader,
    LoadedDecisionContext,
    ReleaseProviderFactory,
    SymbolCloseHistory,
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
    RESEARCH_RELEASE_FEATURE_NAMES,
    STANDARD_PRICE_FEATURE_NAMES,
    explicit_symbol_domain,
    is_st_at_decision,
    research_release_derived_features,
    resolvable_feature_names,
    universe_filter_warnings,
)

if TYPE_CHECKING:
    from finboard_backtest.factor_lab import PriceFeatureProcessPool
    from finboard_backtest.portfolio import CovarianceEstimate
    from finboard_data.factor_lab import FeatureSnapshot
    from finboard_data.releases import FrozenReleaseProvider, ReleasedInstrument

logger = structlog.get_logger(__name__)

#: 信号引擎当前支持的策略类型(其余 kind 继续明确报 not_implemented)。
SIGNAL_ENGINE_STRATEGY_KINDS: frozenset[str] = frozenset({"multi_factor"})

#: 交易日历进程内缓存:provider → 发布交易日历(升序去重)。
#: 发布不可变且 checksum 已由 manifest 冻结锚定,同一 provider 的日历恒定;
#: provider 经工厂按 run memoize(#287),WeakKeyDictionary 让缓存条目随
#: provider 一起被回收 —— 不引入跨 run / 跨事件循环的长命可变状态。
_TRADING_DAYS_CACHE: weakref.WeakKeyDictionary[FrozenReleaseProvider, list[date]] = (
    weakref.WeakKeyDictionary()
)

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
    close_histories: Mapping[str, SymbolCloseHistory | None] | None = None,
) -> dict[str, list[float]]:
    """PIT 门控读取各标的决策时点前可见的完整 close 序列(时间升序)。

    issue #287:提供 ``close_histories``(close 矩阵)时,序列由矩阵前缀切片
    取得,与逐期 PIT 读取逐值等价、不再读盘;矩阵未覆盖的标的回退逐标的
    读取,并以 ``asyncio.gather`` + 信号量并发化(结果与异常都按输入顺序
    组装/抛出,与串行实现一致)。
    """
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    series: dict[str, list[float]] = {}
    pending: list[str] = []
    for code in symbols:
        history = close_histories.get(code) if close_histories else None
        if history is not None:
            values = history.series_until(as_of)
            if values:
                series[code] = values
        else:
            pending.append(code)
    if not pending:
        return series
    semaphore = asyncio.Semaphore(8)

    async def _fetch(code: str) -> list[float]:
        async with semaphore:
            bars = await provider.fetch_point_in_time_bars(
                Symbol(code=code, market=_market_from_value(code)),
                provider.release.period,
                provider.release.start_date,
                as_of.date(),
                decision_at=as_of,
                adjust=provider.release.adjustment,
            )
        return [float(bar.bar.close) for bar in bars]

    results = await asyncio.gather(
        *(_fetch(code) for code in pending), return_exceptions=True
    )
    fetched: dict[str, list[float]] = {}
    for code, result in zip(pending, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        if result:
            fetched[code] = result
    # 按输入顺序合并,保持与串行实现相同的字典插入序。
    for code in symbols:
        if code in fetched:
            series[code] = fetched[code]
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

    issue #287:日历按 provider 进程内缓存(N 期回放此前每期重读完整
    parquet)。只对真实 ``FrozenReleaseProvider`` 启用 —— 其发布不可变、
    日历恒定;其它实现(测试 stub 等)保持每次推导的原行为。
    """
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_data.releases import FrozenReleaseProvider
    from finboard_shared.models import Symbol

    cacheable = isinstance(provider, FrozenReleaseProvider)
    if cacheable:
        cached = _TRADING_DAYS_CACHE.get(provider)
        if cached is not None:
            logger.debug(
                "research_run.trading_calendar_cache_hit",
                release_id=provider.release.release_id,
                days=len(cached),
            )
            return cached

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
            days = sorted({bar.timestamp.date() for bar in bars})
            if cacheable:
                _TRADING_DAYS_CACHE[provider] = days
            return days
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


def single_shot_snapshot_gate_error(
    *,
    strategy_kind: str,
    required_factor_sources: Collection[str],
    frozen_snapshot_count: int,
    parameters: Mapping[str, object],
) -> str | None:
    """入队期 single_shot 缺快照校验:返回拒绝原因,放行返回 None(issue #203)。

    REST 与 MCP 入队共用本口径(报错文案单一来源,不双份漂移):

    * multi_period(显式声明 ``rebalance_frequency=monthly|quarterly``;非法值
      已被 ``ResearchRunQueueIn`` schema 在入队时拒绝)不要求预建快照 ——
      价格因子按发布每日重算,基本面因子仍 PIT 取自冻结快照/研究数据发布;
    * 未声明频率即 single_shot,其决策时点**只能**来自冻结因子快照:依赖
      因子输入或可执行(multi_factor)的策略缺快照时入队秒级拒绝,报错附
      ``execution_mode`` 与缺失因子源,不再等执行期才失败;
    * 其余 kind 由 worker 报 not_implemented,这里不拦截以免遮蔽真正根因。
    """
    frequency = parameters.get("rebalance_frequency")
    if isinstance(frequency, str) and frequency in REBALANCE_FREQUENCIES:
        return None
    if frozen_snapshot_count > 0:
        return None
    sources = sorted(required_factor_sources)
    if sources:
        return (
            "execution_mode=single_shot(未声明 parameters.rebalance_frequency):"
            f"策略依赖因子输入 {sources} 但未冻结 factor_snapshot_ids。"
            "请提供覆盖上述因子源的特征快照,或显式声明 "
            "rebalance_frequency=monthly|quarterly 走多期回放"
            "(多期仅价格因子按发布每日重算,基本面因子仍需冻结快照/研究数据发布提供)"
        )
    if strategy_kind in SIGNAL_ENGINE_STRATEGY_KINDS:
        return (
            "execution_mode=single_shot(未声明 parameters.rebalance_frequency):"
            "该路径的决策时点只能来自冻结因子快照,但 factor_snapshot_ids 为空。"
            "请冻结至少一份特征快照,或声明 rebalance_frequency=monthly|quarterly"
            " 走多期回放"
        )
    if strategy_kind == "user_code":
        # issue #218:user_code 的 decide 数据面来自冻结发布,决策时点与
        # 信号引擎同口径 —— single_shot 取快照 decision_at,缺快照即无决策日。
        return (
            "execution_mode=single_shot(未声明 parameters.rebalance_frequency):"
            "user_code 策略该路径的决策时点只能来自冻结因子快照,但 "
            "factor_snapshot_ids 为空。请声明 rebalance_frequency="
            "monthly|quarterly 走多期回放(decide 每期从冻结发布重算),"
            "或冻结至少一份特征快照以提供决策时点"
        )
    return None


def _multi_period_resolvable_features(
    *,
    research_release_kinds: Collection[object],
    snapshot_feature_names: Collection[str],
) -> frozenset[str]:
    """multi_period 执行期 ``identity`` 节点可解析的数据源集合。

    与 :func:`build_decision_load_contexts` 的实际供给一致:每期价格重算的
    标准价格特征、``close``(价格序列特殊分支)、attached 研究数据发布
    派生的特征(daily_metrics / financial_indicators)与冻结快照观测。
    """
    names: set[str] = set(STANDARD_PRICE_FEATURE_NAMES)
    names.add("close")
    names.update(research_release_derived_features(research_release_kinds))
    names.update(snapshot_feature_names)
    return frozenset(names)


def multi_period_feature_gate_error(
    *,
    identity_sources: Collection[str],
    parameters: Mapping[str, object],
    research_release_kinds: Collection[object],
    snapshot_feature_names: Collection[str] = (),
) -> str | None:
    """入队期 multi_period 特征可用性校验:拒绝原因或 None(放行,issue #253)。

    REST 与 MCP 入队共用本口径(#186/#203 风格):multi_period(显式声明
    ``rebalance_frequency``)的财务 / 自定义因子只能来自 attached 研究数据
    发布或冻结快照,此前「identity 节点缺少数据源」拖到执行期才爆 —— run
    已排队、worker 已开跑。本门控在入队秒级判定:规格 identity 源 ⊆ 多期
    可解析集合,否则具名缺失特征与所需发布 kind。single_shot 不受影响
    (决策时点与特征全部来自快照,由 :func:`single_shot_snapshot_gate_error`
    把关);非法频率值由 ``ResearchRunQueueIn`` schema 拒绝,这里不重复拦。
    """
    frequency = parameters.get("rebalance_frequency")
    if not (isinstance(frequency, str) and frequency in REBALANCE_FREQUENCIES):
        return None
    resolvable = _multi_period_resolvable_features(
        research_release_kinds=research_release_kinds,
        snapshot_feature_names=snapshot_feature_names,
    )
    missing = sorted(set(identity_sources) - resolvable)
    if not missing:
        return None
    feature_to_kinds: dict[str, list[str]] = {}
    for kind_value, names in RESEARCH_RELEASE_FEATURE_NAMES.items():
        for name in names:
            feature_to_kinds.setdefault(name, []).append(kind_value)
    lines: list[str] = []
    no_provider: list[str] = []
    for feature in missing:
        kinds = feature_to_kinds.get(feature)
        if kinds:
            lines.append(f"{feature} 需要 {' / '.join(kinds)} 研究数据发布")
        else:
            no_provider.append(feature)
    if no_provider:
        lines.append(
            f"{no_provider} 无任何发布 kind 可提供:多期仅重算标准价格特征 "
            f"({', '.join(sorted(STANDARD_PRICE_FEATURE_NAMES))}),close 来自"
            "行情;请改用标准价格特征或移除该节点"
        )
    attached = sorted(
        str(getattr(kind, "value", kind)) for kind in research_release_kinds
    )
    return (
        f"execution_mode=multi_period(已声明 rebalance_frequency={frequency}):"
        f"策略引用的特征 {sorted(missing)} 无法由当前冻结发布派生,多期回放"
        "执行期将报「identity 节点缺少数据源」。"
        f"缺失特征所需数据源: {'; '.join(lines)}。"
        f"当前冻结的发布 kind: {attached}。"
        "请在策略验证计划 dataset_release_ids 中附加所需发布后重新入队。"
    )


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
    *,
    process_pool: PriceFeatureProcessPool | None = None,
) -> tuple[FeatureValue, ...]:
    """按单个决策时点从冻结发布重算价格特征(issue #183)。

    复用 ``build_price_feature_snapshot``(与 feature_snapshot 任务同一实现,
    避免双源漂移):PIT 门控读取各标的收盘价,计算 momentum / volatility_Nd /
    downside_volatility,并把观测映射为 ``FeatureValue``(来源绑定冻结 release)。
    冻结因子快照里的基本面特征(如 pb)由 ``FrozenInputLoader`` 另行 PIT 加载,
    两者在 ``build_decision_inputs`` 合并。

    issue #288:``process_pool`` 非空且未损坏时,特征计算在常驻 spawn 进程池
    执行(结果与进程内协程路径逐值相等);池中途损坏(BrokenProcessPool,
    进程被杀 / OOM / pickle 断裂)对本期**具名降级**为进程内协程路径重算一次
    —— 池只是性能优化,不改变结果语义;重算再失败则原样抛(真实数据 /
    代码问题,不吞)。worker 内的业务异常(如数据不足)不是池异常,原样抛。
    """
    from concurrent.futures.process import BrokenProcessPool

    from finboard_backtest.factor_lab import FactorAnalysisError, build_price_feature_snapshot

    def _data_error(exc: FactorAnalysisError) -> ValueError:
        return ValueError(
            f"决策时点 {decision_at.date().isoformat()} 无法从发布重算价格特征"
            f"(通常发布起点历史不足): {exc}"
        )

    explicit_symbols = manifest.strategy_spec.universe.explicit_symbols
    symbols_argument: tuple[str, ...] | None = (
        tuple(explicit_symbols) if explicit_symbols else None
    )
    if process_pool is not None and not process_pool.broken:
        try:
            pool_snapshot = await build_price_feature_snapshot(
                provider=provider,
                decision_at=decision_at,
                code_version=manifest.code_version,
                # issue #254:声明 explicit_symbols 时只重算声明域——universe
                # 过滤域之外的价格特征无消费方,发布全市场重算是纯开销。
                symbols=symbols_argument,
                process_workers=process_pool.worker_count,
                process_executor=process_pool.executor,
            )
        except BrokenProcessPool as exc:
            # 池只是性能优化:损坏对本期与后续期具名降级,不炸 run。
            process_pool.mark_broken()
            logger.warning(
                "research_run.process_pool_degraded",
                stage="execute",
                decision_at=decision_at.date().isoformat(),
                release_id=release_id,
                error=str(exc),
                message="常驻特征计算进程池损坏,本期及后续期降级为进程内协程路径重算",
            )
        except FactorAnalysisError as exc:
            raise _data_error(exc) from exc
        else:
            return _period_feature_values(pool_snapshot, release_id)
    try:
        snapshot = await build_price_feature_snapshot(
            provider=provider,
            decision_at=decision_at,
            code_version=manifest.code_version,
            symbols=symbols_argument,
        )
    except FactorAnalysisError as exc:
        raise _data_error(exc) from exc
    return _period_feature_values(snapshot, release_id)


def _period_feature_values(
    snapshot: FeatureSnapshot,
    release_id: str,
) -> tuple[FeatureValue, ...]:
    """把价格特征快照观测映射为 ``FeatureValue``(两种执行路径共用,零漂移)。"""
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
    """读取各标的全区间收盘价映射(非 PIT;收盘价在当日收盘即公开)。

    issue #287:逐 symbol 串行改 ``asyncio.gather`` + 信号量;异常按输入
    顺序抛出,与串行实现逐值一致。
    """
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(8)

    async def _fetch(code: str) -> dict[date, Decimal]:
        async with semaphore:
            bars = await provider.fetch_bars(
                Symbol(code=code, market=_market_from_value(code)),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                adjust=provider.release.adjustment,
            )
        return {bar.timestamp.date(): bar.close for bar in bars}

    results = await asyncio.gather(
        *(_fetch(code) for code in symbols), return_exceptions=True
    )
    fetched: dict[str, dict[date, Decimal]] = {}
    for code, result in zip(symbols, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        fetched[code] = result
    return fetched


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
    instruments: Sequence[ReleasedInstrument],
    context: LoadedDecisionContext,
    features_by_source: Mapping[str, Mapping[str, float]],
) -> tuple[SpecUniverseCandidate, ...]:
    """把发布标的映射为 ``UniverseSpec`` 候选(近似字段见函数体)。

    字段近似:listing_days 来自 list_date;price 来自决策日 close;停牌按
    发布快照静态近似(suspended_sessions);``is_st`` 按发布 instruments 的
    ``name_history`` 区间取决策日名称 PIT 判定(issue #213,无覆盖区间回退
    当前名称近似);``market_cap`` 来自 daily_metrics 的特征观测。

    ``instruments`` 由调用方按 ``explicit_symbols`` 收窄(issue #254)。
    """
    candidates: list[SpecUniverseCandidate] = []
    decision_date = context.business_date
    for instrument in instruments:
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
        market_cap = fields.get("market_cap")
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
                market_cap=(
                    float(market_cap) if isinstance(market_cap, (int, float)) else None
                ),
                suspended=instrument.suspended_sessions > 0,
                delisted=delisted,
                is_st=is_st_at_decision(instrument, decision_date),
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
    """``UniverseSpec`` 过滤候选池(ranking / selection_limit / 排除规则)。

    issue #254:声明 ``explicit_symbols`` 时评估域收窄为 explicit ∩ 发布
    标的(与静态预检共用 ``explicit_symbol_domain``,两边一致);声明但
    发布中缺失的标的随降级 warning 具名声明,不静默。
    """
    domain, missing_explicit = explicit_symbol_domain(
        spec.universe, provider.release.instruments
    )
    if missing_explicit:
        logger.warning(
            "research_run.universe_explicit_symbol_missing",
            decision_date=context.business_date.isoformat(),
            missing_count=len(missing_explicit),
            missing_symbols=missing_explicit[:20],
            message="explicit_symbols 声明的标的不在发布 instruments 中,已按缺失处理",
        )
    decisions = explain_universe(
        spec.universe, _spec_universe_candidates(domain, context, features_by_source)
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
        decision_date=decision_at.date(),
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
    """执行期空池错误的根因信息:排除统计 + 缺失字段名(issue #186 / #213)。

    issue #254:声明 ``explicit_symbols`` 时诊断域与过滤域一致收窄,
    ``list_date`` 缺失统计不再误报声明域之外的标的。
    """
    domain, missing_explicit = explicit_symbol_domain(spec.universe, instruments)
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
        elif reason == "missing_market_cap":
            missing.add("market_cap")
        elif reason == "listing_age_below_minimum" and any(
            getattr(item, "list_date", None) is None for item in domain
        ):
            missing.add("list_date")
        elif reason == "delisted":
            missing.add("delist_date")
    missing_text = "、".join(sorted(missing)) if missing else "无"
    explicit_note = ""
    if spec.universe.explicit_symbols:
        explicit_note = (
            f"(explicit_symbols 声明 {len(spec.universe.explicit_symbols)} 个,"
            f"其中 {len(missing_explicit)} 个不在发布中)"
        )
    return (
        f"决策日 {decision_at.date().isoformat()} 候选池为空: "
        f"共 {len(candidates)} 个候选标的全部被过滤{explicit_note}。"
        f"排除统计: {stats or '无'};缺失字段: {missing_text}。"
        "请检查发布 instrument 元数据(list_date 等)与冻结特征是否齐备,"
        "或放宽 universe 过滤条件。"
    )


@dataclass(frozen=True, slots=True)
class DecisionLoadContext:
    """单个决策时点的机械加载结果 + 可信号化标的(issue #218 拆出)。

    ``build_decision_inputs``(multi_factor 信号引擎)与 ``user_code``
    沙箱策略适配器共用同一加载路径:决策日推导、PIT 特征、universe 过滤、
    价格序列与协方差;差异只在「信号」如何产生 —— 前者按 feature_graph
    求值,后者调沙箱 decide。快照 id 供信号锚定(single_shot)。
    """

    context: LoadedDecisionContext
    candidates: tuple[UniverseCandidate, ...]
    features: tuple[FeatureValue, ...]
    signalable: frozenset[str]
    price_series: dict[str, list[float]]
    covariance: CovarianceEstimate | None
    snapshot_id: str | None


#: 加载期分块大小(issue #288):分块内的期次并发 gather,分块之间顺序推进,
#: 结果按原始期序归位。取值权衡:足以让常驻特征进程池(默认 4 worker)与
#: 线程池并发读持续有活干(每期内部各自还有 8 并发读),又不至于让过多期的
#: 全市场上下文(候选 / 价格 / 特征快照)同时驻留;4 x 8 = 至多 32 个在途
#: 读取任务,由默认线程池上限自然钳制,不与 #287 的并发读无界叠加。
_DECISION_LOAD_CHUNK = 4


#: 加载期分块探针(issue #306,进度形态 #308):(done, total) → None。
#: ``done`` = 已完成加载的决策期数,``total`` = 推导出的决策期总数。
#: 实现方(:func:`build_run_interrupt_probe`,构造于 adapter 工厂、持有 DB
#: 会话)承担两件事:(1) 轮询 run status 与 job cancel_requested,发现外部
#: 打断即抛 :class:`ResearchRunInterruptedError`(coordinator 走既有
#: INTERRUPTED → job retry_waiting 自动重试路径);(2) 尽力而为把加载进度
#: ``research_run:decision_load k/N`` 写入 background_jobs 的 **phase 字段**
#: (不写 done/total 数值 —— #188 的数值口径是「stage x decision」计数,
#: 混写两种口径会被 ``update_progress`` 的单调/夹紧规则互相污染),让
#: worker 维护路径的「无进展检测」在加载期也有进展可判,正常长加载不被
#: 误杀,job 视图实时可见 k/N 推进。
LoadChunkProbe = Callable[[int, int], Awaitable[None]]


def build_run_interrupt_probe(
    session_maker: async_sessionmaker[Any],
    run_id: str,
) -> LoadChunkProbe:
    """构造加载期分块探针(issue #306;进度形态 #308):轮询 + k/N 进度上报。

    僵尸场景(RR-7a74):run 行被外部标 interrupted 后,加载期毫无感知、
    心跳照常续租,job 永不回收。本探针在每个分块边界做毫秒级一次的只读
    轮询:run 非 RUNNING(被打断/取消)或 job 被 request_cancel 即抛
    :class:`ResearchRunInterruptedError`。存活时尽力把加载进度以
    ``research_run:decision_load k/N``(**k/N 只进 phase 字段**,done/total
    数值列保持 #188 决策执行口径不被污染,#308)写入 background_jobs
    (单条 UPDATE,失败静默 —— 可观测性不阻断执行)。DB 会话按次开关,
    不跨块持有。
    """

    async def _probe(done: int, total: int) -> None:
        from finboard_persistence import (
            BackgroundJobRepository,
            ResearchRunRepository,
        )

        async with session_maker() as session:
            row = await ResearchRunRepository(session).get(run_id)
            if row is None:
                raise ResearchRunInterruptedError(f"运行记录在加载期消失: {run_id}")
            if row.status == "cancelled":
                raise ResearchRunInterruptedError(
                    f"运行已被取消(cancelled),中止加载: {run_id}"
                )
            if row.status != "running":
                raise ResearchRunInterruptedError(
                    f"运行状态已变为 {row.status}(外部打断),中止加载: {run_id}"
                )
            job_id = row.job_id
            job_status: str | None = None
            if job_id is not None:
                job_row = await BackgroundJobRepository(session).get(job_id)
                job_status = None if job_row is None else job_row.status
        if job_status == "cancel_requested":
            raise ResearchRunInterruptedError(
                f"后台任务被请求取消(cancel_requested),中止加载: {run_id}"
            )
        if job_id is None:
            return
        # 加载进度上报(尽力而为,issue #306/#308):k/N 编码进 phase,
        # done/total 数值列不动(避免 #188 口径被加载期数值占位)。僵尸无
        # 进展检测(#306)由此在加载期获得判定依据;update_progress 顺带
        # 刷新 heartbeat_at,长加载期间心跳与进度同源推进。
        with contextlib.suppress(Exception):
            async with session_maker() as session:
                repo = BackgroundJobRepository(session)
                await repo.update_progress(
                    job_id,
                    done=0,
                    total=None,
                    phase=f"research_run:decision_load {done}/{total}",
                )
                await repo.checkpoint()

    return _probe


async def _start_period_feature_pool(
    provider: FrozenReleaseProvider,
    process_workers: int,
) -> PriceFeatureProcessPool | None:
    """启动本次加载期常驻的价格特征计算进程池(issue #288)。

    池是纯性能优化:构建 / 预热失败(spawn 环境损坏、发布初始化失败等)不把
    run 打挂 —— 记 ``research_run.process_pool_start_failed`` 具名 warning 后
    返回 ``None``,本次加载全部期次降级为进程内协程路径(结果逐值一致)。
    返回非 ``None`` 时调用方负责在加载结束(含异常)后 ``aclose()``。
    """
    from finboard_backtest.factor_lab import (
        PRICE_FEATURE_POOL_MAX_TASKS_PER_CHILD,
        PriceFeatureProcessPool,
    )

    pool = PriceFeatureProcessPool(provider=provider, worker_count=process_workers)
    try:
        await pool.start()
    except Exception as exc:
        logger.warning(
            "research_run.process_pool_start_failed",
            stage="decision_load",
            worker_count=process_workers,
            release_id=provider.release.release_id,
            error=str(exc),
            message="常驻特征计算进程池启动失败,本次加载全部期次降级为进程内协程路径",
        )
        return None
    logger.info(
        "research_run.process_pool_started",
        stage="decision_load",
        worker_count=process_workers,
        release_id=provider.release.release_id,
        max_tasks_per_child=PRICE_FEATURE_POOL_MAX_TASKS_PER_CHILD,
    )
    return pool


async def build_decision_load_contexts(
    manifest: ResearchRunManifest,
    *,
    release_provider_factory: ReleaseProviderFactory,
    snapshot_provider: FeatureSnapshotProvider,
    process_workers: int = 0,
    chunk_probe: LoadChunkProbe | None = None,
) -> tuple[DecisionLoadContext, ...]:
    """按执行模式加载全部决策的机械上下文(不含信号,issue #218)。

    single_shot:决策日序列 = manifest.factor_snapshots 的 ``decision_at``
    排序去重;multi_period:决策日由 ``parameters.rebalance_frequency``
    按冻结发布交易日历推导,每期重算 price features 并合并冻结快照 PIT
    观测。universe 过滤 / 空池根因 / 降级 warning 语义与信号引擎一致。

    issue #288:逐期加载按 ``_DECISION_LOAD_CHUNK`` 分块 ``gather`` 并行 ——
    纯读,结果按原始期序归位(contexts 顺序 / 产物 checksum 不变);
    ``process_workers`` > 0 且 multi_period 时,逐期价格特征经常驻 spawn
    进程池计算,池构建失败 / 中途损坏均具名降级为进程内协程路径。
    #263 逐期失败标记:分块内 ``return_exceptions=True`` 收集后按原始期序
    重抛**第一个**异常(与串行首个失败一致),挂标记后类型 / 消息不变。

    issue #306:``chunk_probe``(可选)在每个分块边界(块内异常处理之后、
    下一块 gather 之前)调用 —— 轮询 run status / job cancel_requested,
    外部打断抛 ``ResearchRunInterruptedError`` 并尽力上报加载进度。打断是
    协作取消信号而非数据失败,不经 #263 的 ``attach_decision_load_context``
    标记路径。

    issue #308:加载进度以 ``research_run:decision_load k/N`` 编码进 job 的
    phase 字段(k=已完成期数、N=推导出的决策期总数,multi_period 与
    single_shot 同机制);首帧(k=0)在 close 矩阵预建**之前**上报,覆盖
    预建这段此前零进度的空白窗。
    """
    if not manifest.dataset_releases:
        raise ValueError("manifest 必须冻结至少一个数据发布")
    release_ref = _bars_release_ref(manifest, release_provider_factory)
    provider = release_provider_factory(release_ref.artifact_id)
    loader = FrozenInputLoader(
        release_provider_factory=release_provider_factory,
        snapshot_provider=snapshot_provider,
    )
    frequency = _rebalance_frequency(manifest)

    if frequency is not None:
        decision_days = await _derive_rebalance_decision_days(provider, frequency)
        if not decision_days:
            # issue #203:区分根因 —— 频率已声明(multi_period)但发布日历推导不出
            # 任何决策时点,与「未声明频率缺快照」是两回事,不能混报。
            raise ValueError(
                f"execution_mode=multi_period:已声明 rebalance_frequency={frequency},"
                "但冻结发布交易日历未能推导出任何决策时点(每期期末之后必须存在"
                "下一交易日才能执行成交)。请检查发布区间是否覆盖至少一个完整"
                "周期期末,或延长发布区间后重新入队。"
            )
    else:
        decision_days = await _snapshot_decision_days(manifest, snapshot_provider)
        if not decision_days:
            raise ValueError(
                "execution_mode=single_shot:未声明 parameters.rebalance_frequency,"
                "该路径的决策时点只能来自冻结因子快照,但 manifest.factor_snapshots"
                " 为空。请入队时冻结 factor_snapshot_ids,或显式声明 "
                "rebalance_frequency=monthly|quarterly 走多期回放(多期仅价格因子"
                "按发布每日重算,基本面因子仍 PIT 取自冻结快照/研究数据发布)。"
            )

    # issue #288:日历与 close 矩阵(#287 的惰性进程内缓存)在进入分块并行
    # 前预建 —— 并发首建只会重复读盘且打穿矩阵收益,预建收敛到单一顺序点。
    # multi_period 的日历在决策推导时已缓存,这里是 single_shot 的兜底预热。
    # issue #308:首帧探针在预建**之前**触发 —— 决策日总数已推导完成,先把
    # ``decision_load 0/N`` 透出到 job 视图,覆盖日历预热 + close 矩阵全区间
    # 读取这段此前完全无进度的最长空白窗(RR-7a74 的「数小时 0/0」)。
    if chunk_probe is not None and decision_days:
        await chunk_probe(0, len(decision_days))
    await _release_trading_days(provider)
    await loader.ensure_close_histories(manifest)

    pool: PriceFeatureProcessPool | None = None
    if frequency is not None and process_workers > 0:
        pool = await _start_period_feature_pool(provider, process_workers)

    async def _load_one(
        decision_at: datetime, snapshot_id: str | None
    ) -> DecisionLoadContext:
        execution_at = await _next_execution_at(provider, decision_at)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        if frequency is not None:
            period_features = await _compute_period_features(
                provider,
                manifest,
                decision_at,
                release_ref.artifact_id,
                process_pool=pool,
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
        price_series = await _load_price_series(
            provider, tuple(signalable), decision_at,
            close_histories=loader.close_histories,
        )
        signalable = frozenset(
            symbol for symbol in signalable if len(price_series.get(symbol, ())) >= 2
        )
        return DecisionLoadContext(
            context=context,
            candidates=candidates,
            features=features,
            signalable=signalable,
            price_series=price_series,
            # 协方差估计是纯 CPU 段(numpy 矩阵运算),经 to_thread 卸载
            # (issue #286);price_series 为普通数据,线程间无共享可变态。
            # 分块 gather(issue #288)下各期的卸载调用互不共享状态,安全。
            covariance=await asyncio.to_thread(_estimate_covariance, price_series),
            snapshot_id=snapshot_id,
        )

    contexts: list[DecisionLoadContext] = []
    try:
        for chunk_start in range(0, len(decision_days), _DECISION_LOAD_CHUNK):
            # issue #306:分块边界探针 —— 打断(外部 interrupted / cancel_requested)
            # 在进入下一块前秒级感知;加载进度 k/N 尽力上报(#308 phase 编码)。
            # 首块边界跳过(k=0 已由预建前的首帧上报,见上),避免重复帧;
            # 探针自身抛错原样透传,不经 #263 数据失败标记路径。
            if chunk_probe is not None and contexts:
                await chunk_probe(len(contexts), len(decision_days))
            chunk = decision_days[chunk_start : chunk_start + _DECISION_LOAD_CHUNK]
            results = await asyncio.gather(
                *(_load_one(decision_at, snapshot_id) for decision_at, snapshot_id in chunk),
                return_exceptions=True,
            )
            # issue #263:按原始期序收集 —— 第一个失败期(与串行首个失败一致)
            # 在 bare raise 前挂决策标记;异常类型 / 消息 / traceback 不被改写,
            # runner 通用收口经 read_decision_load_context 读回失败期次。
            for ((decision_at, _), result) in zip(chunk, results, strict=True):
                if isinstance(result, BaseException):
                    if isinstance(result, Exception):
                        attach_decision_load_context(
                            result,
                            decision_at=decision_at,
                            release_id=release_ref.artifact_id,
                        )
                    raise result
                contexts.append(result)
    finally:
        if pool is not None:
            await pool.aclose()
    return tuple(contexts)


async def build_decision_inputs(
    manifest: ResearchRunManifest,
    *,
    release_provider_factory: ReleaseProviderFactory,
    snapshot_provider: FeatureSnapshotProvider,
    process_workers: int = 0,
    chunk_probe: LoadChunkProbe | None = None,
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

    ``process_workers``(issue #288)> 0 且 multi_period 时,逐期价格特征
    经常驻 spawn 进程池计算;结果与进程内路径逐值相等。
    """
    frequency = _rebalance_frequency(manifest)
    # 信号求值(特征图 + 规则)是逐决策的纯 CPU 密集段,经 asyncio.to_thread
    # 卸载(issue #286):长计算不再阻塞事件循环线程,worker 心跳可续约。
    # build_normalized_signals 为模块级纯函数,普通数据入参,线程间无共享可变态。
    inputs: list[PortfolioDecisionInput] = []
    for loaded in await build_decision_load_contexts(
        manifest,
        release_provider_factory=release_provider_factory,
        snapshot_provider=snapshot_provider,
        process_workers=process_workers,
        chunk_probe=chunk_probe,
    ):
        signals = await asyncio.to_thread(
            build_normalized_signals,
            manifest.strategy_spec,
            features=loaded.features,
            prices=loaded.context.prices,
            included_symbols=loaded.signalable,
            factor_snapshot_id=None if frequency is not None else loaded.snapshot_id,
            price_series=loaded.price_series,
        )
        inputs.append(
            PortfolioDecisionInput(
                business_date=loaded.context.business_date,
                decision_at=loaded.context.decision_at,
                execution_at=loaded.context.execution_at,
                candidates=loaded.candidates,
                features=loaded.features,
                signals=signals,
                prices=loaded.context.prices,
                execution_prices=loaded.context.execution_prices,
                lot_info=loaded.context.lot_info,
                input_artifact_ids=loaded.context.input_artifact_ids,
                covariance=loaded.covariance,
            )
        )
    return tuple(inputs)


def _bars_release_ref(
    manifest: ResearchRunManifest,
    release_provider_factory: ReleaseProviderFactory,
) -> FrozenArtifactRef:
    """取 bars 主发布引用(issue #187:联合发布按 kind 融合)。

    行情 / 候选池 / 执行元数据必须来自唯一的 bars 发布;daily_metrics /
    financial_indicators 研究数据发布只提供因子观测,不作为主发布。
    """
    from finboard_data.releases import ReleaseDatasetKind

    bars_refs = []
    for release_ref in manifest.dataset_releases:
        provider = release_provider_factory(release_ref.artifact_id)
        if provider.release.dataset_kind is ReleaseDatasetKind.BARS:
            bars_refs.append(release_ref)
    if len(bars_refs) != 1:
        raise ValueError(
            "联合发布必须恰好包含一个 bars 主发布(行情/候选池来源),"
            "实际: " + ",".join(ref.artifact_id for ref in manifest.dataset_releases)
        )
    return bars_refs[0]


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
        process_workers: int = 0,
        chunk_probe: LoadChunkProbe | None = None,
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
        # issue #288:multi_period 逐期价格特征使用的常驻进程池 worker 数
        # (settings ``research_price_feature_process_workers``;0 = 进程内)。
        self._process_workers = max(0, process_workers)
        # issue #306:加载期分块探针(run status / cancel 轮询 + 进度上报)。
        self._chunk_probe = chunk_probe
        self._inputs: tuple[PortfolioDecisionInput, ...] | None = None
        self._equity_curve: tuple[EquityPoint, ...] = ()
        self._benchmark_curve: tuple[tuple[date, Decimal], ...] = ()
        # issue #217:用户因子 screen 指标(决策消费后计算,report 阶段取用)。
        self._factor_screen: dict[str, Any] | None = None

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
                process_workers=self._process_workers,
                chunk_probe=self._chunk_probe,
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
            release_ref = _bars_release_ref(manifest, self._release_provider_factory)
            provider = self._release_provider_factory(release_ref.artifact_id)
            self._equity_curve = await build_daily_equity_curve(provider, manifest, collected)
        # 基准曲线(issue #184):两种执行模式都按 benchmark_config.symbol
        # 从冻结发布取行情;缺失落空曲线,由 build_report 记 warning。
        self._benchmark_curve = await _load_benchmark_curve(
            manifest, self._release_provider_factory
        )
        # issue #217:用户因子 screen 指标 —— 尽力而为,计算失败只记
        # warning 不把 run 打挂(展示层指标,不影响决策/账本语义)。
        # issue #304:组合阶段拒绝时由 compute_partial_evidence 走同一补算。
        await self._compute_factor_screen(manifest)

    async def _compute_factor_screen(self, manifest: ResearchRunManifest) -> str | None:
        """用户因子 screen 指标(issue #217/#304):尽力而为,幂等。

        成功把结果暂存 ``self._factor_screen``(report 阶段取用)并返回
        None;失败记具名 warning 并返回失败原因(不把 run 打挂)。已有
        结果不重算。
        """
        if self._factor_screen is not None or not self._inputs:
            return None
        try:
            from finboard_backtest.research_run.factor_screen import (
                build_factor_screen,
            )

            self._factor_screen = await build_factor_screen(
                manifest,
                self._inputs,
                self._release_provider_factory,
            )
            return None
        except Exception as exc:
            logger.warning(
                "factor_screen_computation_failed",
                run_id=manifest.run_id,
                exc_info=True,
            )
            return f"factor_screen_computation_failed: {exc}"

    async def compute_partial_evidence(
        self,
        manifest: ResearchRunManifest,
        *,
        completed_decisions: int,
    ) -> dict[str, JsonValue] | None:
        """组合阶段硬约束拒绝后的部分证据补算(issue #304)。

        factor_screen 只依赖已构建的冻结决策输入(``self._inputs``),与组合
        阶段是否失败无关;在 run 被拒绝前尽力补算并暂存,``build_report``
        照常携带。返回 partial 标记(失败决策 1-based 定位 + 补算 warning),
        无冻结输入可补算时返回 None。本方法不改变失败语义,只保留不依赖
        组合阶段的证据;失败决策的日期取自同序号的冻结输入(该决策未产出,
        runner 只知道已完成期数)。
        """
        if not self._inputs:
            return None
        marker: dict[str, JsonValue] = {
            "completed_decisions": completed_decisions,
            "failed_decision_index": completed_decisions + 1,
        }
        if completed_decisions < len(self._inputs):
            marker["decision_date"] = self._inputs[
                completed_decisions
            ].business_date.isoformat()
        warnings: list[JsonValue] = []
        screen_failure = await self._compute_factor_screen(manifest)
        if screen_failure is not None:
            warnings.append(screen_failure)
        if warnings:
            marker["warnings"] = warnings
        return marker

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionBundle],
    ) -> ResearchRunReport:
        delegate = PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._inputs or (),
        )
        report = delegate.build_report(
            manifest,
            decisions,
            equity_curve=self._equity_curve,
            benchmark_curve=self._benchmark_curve,
        )
        if self._factor_screen is not None:
            report = replace(report, factor_screen=self._factor_screen)
        return report


def _resolve_period_feature_process_workers(
    settings_factory: Callable[[], Any] | None,
) -> int:
    """从 settings 解析 research 逐期价格特征进程池 worker 数(issue #288)。

    ``research_price_feature_process_workers``(默认 4,0 = 关闭);settings
    工厂缺失 / 抛错 / 返回 None 一律按 0(进程内协程路径)处理 —— 该配置
    只是性能开关,解析失败不能阻断 run 入口。
    """
    if settings_factory is None:
        return 0
    try:
        settings = settings_factory()
    except Exception:
        logger.warning(
            "research_run.process_workers_settings_unavailable",
            message="settings 工厂抛错,逐期价格特征多进程关闭",
        )
        return 0
    if settings is None:
        return 0
    try:
        value = int(getattr(settings, "research_price_feature_process_workers", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def build_signal_engine_adapter_factory(
    session_maker: async_sessionmaker[Any],
    *,
    release_root: str | Path | None = None,
    settings_factory: Callable[[], Any] | None = None,
) -> Callable[[ResearchRunManifest], ResearchStrategyAdapter]:
    """构造 CLI 可注入的 ``AdapterFactory``(multi_factor / user_code 分发)。

    延迟导入 persistence / data 依赖(finboard-backtest 不直接依赖
    finboard-persistence);其余规格继续明确报 not_implemented。
    ``settings_factory`` 供 user_code 沙箱调用方解析镜像/资源限制/代码仓库
    路径(#218)与 multi_period 逐期特征进程池 worker 数(#288);缺省时
    user_code 运行在加载期报 sandbox 未启用、特征走进程内协程路径。
    """

    def _factory(manifest: ResearchRunManifest) -> ResearchStrategyAdapter:
        from finboard_data.releases import FrozenReleaseProvider
        from finboard_persistence import FeatureSnapshotRepository

        root = Path(
            release_root
            if release_root is not None
            else os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
        )

        # manifest 冻结的 release checksum 即 DB release_checksum(issue #202):
        # 锚定校验替代代码版本敏感的整清单重算,worker 与发布端代码漂移不误伤。
        release_checksums = {
            ref.artifact_id: ref.checksum for ref in manifest.dataset_releases
        }

        # issue #287:provider 按 release_id 在本 run(工厂闭包)内 memoize。
        # 发布不可变且 checksum 已由 manifest 锚定,实例复用使逐文件 SHA256
        # 校验缓存(_verified)与 manifest 加载每发布只付一次 —— 此前 loader
        # 每决策 3 处调用工厂,per-instance 校验缓存被打穿、每次重验整文件。
        # 缓存生命周期 = 闭包生命周期 = 单次 run 适配器,无跨 run / 跨事件
        # 循环共享的可变状态。
        provider_memo: dict[str, FrozenReleaseProvider] = {}

        def _release_factory(release_id: str) -> FrozenReleaseProvider:
            provider = provider_memo.get(release_id)
            if provider is not None:
                logger.debug(
                    "research_run.release_provider_reused", release_id=release_id
                )
                return provider
            provider = FrozenReleaseProvider(
                release_root=root,
                release_id=release_id,
                expected_checksum=release_checksums.get(release_id),
            )
            provider_memo[release_id] = provider
            logger.debug("research_run.release_provider_created", release_id=release_id)
            return provider

        async def _snapshot_provider(snapshot_id: str) -> object:
            async with session_maker() as session:
                return await FeatureSnapshotRepository(session).get(snapshot_id)

        # issue #306:加载期分块探针 —— run status / job cancel_requested 轮询 +
        # 加载进度上报。打断路径(run 被外部标 interrupted 等)在此秒级感知,
        # 不再出现「run 已 interrupted、job 靠心跳续租僵死 7.5 小时」的僵尸。
        # 按 manifest.run_id 在工厂闭包内构造,与 provider memo 同生命周期。
        chunk_probe = build_run_interrupt_probe(session_maker, manifest.run_id)

        if manifest.strategy_kind == "user_code":
            from finboard_backtest.research_run.user_code_engine import (
                UserCodeStrategyAdapter,
            )

            return UserCodeStrategyAdapter(
                manifest=manifest,
                release_provider_factory=_release_factory,
                snapshot_provider=_snapshot_provider,  # type: ignore[arg-type]
                settings_factory=settings_factory,
                chunk_probe=chunk_probe,
            )

        if manifest.strategy_kind not in SIGNAL_ENGINE_STRATEGY_KINDS:
            from finboard_backtest.background_jobs.contracts import ExecutorError

            raise ExecutorError(
                code="signal_engine_not_implemented",
                summary=(
                    f"信号引擎当前仅支持 {sorted(SIGNAL_ENGINE_STRATEGY_KINDS)}"
                    f"+user_code 规格;{manifest.strategy_kind} 尚未实现"
                ),
                retryable=False,
            )

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=_release_factory,
            snapshot_provider=_snapshot_provider,  # type: ignore[arg-type]
            process_workers=_resolve_period_feature_process_workers(settings_factory),
            chunk_probe=chunk_probe,
        )

    return _factory


__all__ = [
    "SIGNAL_ENGINE_STRATEGY_KINDS",
    "DecisionLoadContext",
    "LoadChunkProbe",
    "SignalEnginePipelineAdapter",
    "build_daily_equity_curve",
    "build_decision_inputs",
    "build_decision_load_contexts",
    "build_normalized_signals",
    "build_run_interrupt_probe",
    "build_signal_engine_adapter_factory",
    "evaluate_feature_graph",
    "evaluate_signal_rules",
    "single_shot_snapshot_gate_error",
]
