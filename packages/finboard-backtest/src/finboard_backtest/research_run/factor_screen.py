"""research run 因子筛选(screen)指标(issue #217)。

对 run 实际引用的**用户自定义因子**(``u_`` 前缀,沙箱执行产出的快照
观测)计算机器筛选指标并写入 run report 的 ``factor_screen`` 段,支撑
自主因子挖掘闭环(假设→提交→执行→screen→复合→OOS):

* **IC / IR** —— 每期决策时点因子横截面与下一期 forward return 的
  spearman 秩相关;``rank_ic`` 为序列均值,``rank_ic_ir`` = 均值/标准差
  (样本 >= 2 期才有,单期 run 记 None);
* **分层收益** —— 每期按因子值升序分 5 桶(``np.array_split``),桶内
  等权 forward return,报告跨期平均;Q1=低值桶,Q5=高值桶;
* **换手率** —— 相邻期最高值桶(Q5)成员变化率(|curr-prev| / |并集|)
  的均值;单期 run 记 None;
* **相关性矩阵** —— 每期 user 因子与同截面其他特征(builtin 因子 /
  价格特征,来自冻结快照观测与研究发布派生)的 spearman,跨期平均,
  作为与既有因子库的对照。

forward return 窗口:相邻决策时点各自「PIT 可见最新 close」之比;
最后一期到 bars 主发布区间末(end_date 收盘)。计算失败(价格缺失等)
记入 ``issues`` 不中断 run —— screen 是展示层指标,尽力而为。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import Any

import numpy as np

from finboard_backtest.research_run.contracts import (
    ResearchRunManifest,
)
from finboard_backtest.research_run.portfolio_pipeline import PortfolioDecisionInput
from finboard_data.factor_lab import is_user_factor_name

#: 分层桶数(Q1..Q5)
QUANTILES = 5
#: spearman 最小共同标的数(与 factors.analysis 同口径)
_MIN_OVERLAP = 5
#: IR / 换手需要的最少期数
_MIN_PERIODS = 2

_METHOD_NOTE = (
    "spearman rank ic;forward window=相邻决策时点PIT最新close"
    f"(末期为发布区间末);quantiles={QUANTILES}"
)


async def build_factor_screen(
    manifest: ResearchRunManifest,
    inputs: Sequence[PortfolioDecisionInput],
    release_provider_factory: Any,
) -> dict[str, Any] | None:
    """对 run 引用的用户因子计算 screen 指标;无用户因子返回 None。

    issue #463 兼容入口:逐期投影横截面后转调
    :func:`build_factor_screen_from_periods`;流式调用方(signal_engine
    逐期拉取输入)应直接捕获投影并调用后者。
    """
    return await build_factor_screen_from_periods(
        manifest,
        [_period_cross_section(item) for item in inputs],
        release_provider_factory,
    )


async def build_factor_screen_from_periods(
    manifest: ResearchRunManifest,
    periods: Sequence[dict[str, Any]],
    release_provider_factory: Any,
) -> dict[str, Any] | None:
    """对预投影横截面(issue #463:``_period_cross_section`` 产物)计算
    screen 指标;无期次或无用户因子返回 None。"""
    if not periods:
        return None
    user_factors: set[str] = set()
    for period in periods:
        user_factors.update(period["user"])
    if not user_factors:
        return None

    forward_closes = await _forward_close_series(
        manifest, periods, release_provider_factory
    )
    forward_returns = _forward_returns(periods, forward_closes)

    issues: list[str] = []
    result_factors: dict[str, Any] = {}
    for factor in sorted(user_factors):
        result_factors[factor] = _screen_one_factor(factor, periods, forward_returns, issues)
    baselines = sorted({name for period in periods for name in period["others"]})
    return {
        "factors": result_factors,
        "correlation_baselines": baselines,
        "n_periods": len(periods),
        "decision_points": [item["decision_at"].isoformat() for item in periods],
        "method": _METHOD_NOTE,
        "issues": issues,
    }


async def build_strategy_screen(
    manifest: ResearchRunManifest,
    inputs: Sequence[PortfolioDecisionInput],
    target_weights: Sequence[Mapping[str, float]],
    release_provider_factory: Any,
) -> dict[str, Any] | None:
    """对 user_code 每期目标权重计算同口径 screen 指标。

    策略没有单独的因子观测列,这里把每期 ``targets`` 视为一个截面评分
    ``__strategy__`` 后复用 IC/换手/相关性实现。目标权重只是研究输入,
    不会在本函数中生成订单或修改组合状态。

    issue #463 兼容入口:逐期投影横截面后转调
    :func:`build_strategy_screen_from_periods`。
    """
    return await build_strategy_screen_from_periods(
        manifest,
        [_period_cross_section(item) for item in inputs],
        target_weights,
        release_provider_factory,
    )


async def build_strategy_screen_from_periods(
    manifest: ResearchRunManifest,
    periods: Sequence[dict[str, Any]],
    target_weights: Sequence[Mapping[str, float]],
    release_provider_factory: Any,
) -> dict[str, Any] | None:
    """对预投影横截面 + user_code 每期目标权重计算 screen 指标(#463)。"""
    if not periods or len(periods) != len(target_weights):
        return None
    strategy_symbols = {
        str(symbol)
        for weights in target_weights
        for symbol, value in weights.items()
        if _finite_weight(value)
    }
    if not strategy_symbols:
        return None
    for period, weights in zip(periods, target_weights, strict=True):
        period["user"]["__strategy__"] = {
            str(symbol): float(value) for symbol, value in weights.items() if _finite_weight(value)
        }

    forward_closes = await _forward_close_series(
        manifest,
        periods,
        release_provider_factory,
        symbols=strategy_symbols,
    )
    forward_returns = _forward_returns(periods, forward_closes)
    issues: list[str] = []
    metrics = _screen_one_factor("__strategy__", periods, forward_returns, issues)
    metrics["origin"] = "user_code"
    return {
        "strategy": metrics,
        "correlation_baselines": sorted({name for period in periods for name in period["others"]}),
        "n_periods": len(periods),
        "decision_points": [item["decision_at"].isoformat() for item in periods],
        "method": _METHOD_NOTE + ";score=target_weight",
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# 横截面与 forward return
# --------------------------------------------------------------------------- #


def manifest_declares_user_factors(manifest: ResearchRunManifest) -> bool:
    """manifest 是否声明了用户因子(``u_`` 前缀;保守超集判定,#470 前半场)。

    ``u_`` 因子名只可能来自 manifest 携带的声明(策略 spec / 因子快照 /
    因子序列引用 / 组合风险因子限制等),对其 canonical JSON 做标记扫描即
    得保守超集:误报只多付横截面投影的捕获成本(正确性不变 —— 无 ``u_``
    观测时 screen 照旧返回 None),漏报由 ``_period_cross_section`` 的
    「当期 ``user`` 桶非空即回退全量投影」兜底。全市场 run 的 ``others``
    投影 ~15MB/期(30 特征 x 5000+ 标的),556 期 ≈ 8GB —— 无用户因子的
    run 不该为恒为 None 的 screen 付这笔驻留。
    """

    import re

    from finboard_backtest.research_run.contracts import canonical_json

    return re.search(r'"u_[A-Za-z0-9_.\-]+"', canonical_json(manifest)) is not None


def _period_cross_section(
    item: PortfolioDecisionInput,
    *,
    include_series: bool = True,
) -> dict[str, Any]:
    """把一期决策输入按 user / 其他特征重组为横截面字典(#463 投影)。

    投影只保留 screen 计算所需的小数据(features 派生值 / 决策时点 /
    决策价 / 候选池 symbol→market),供流式调用方逐期捕获后丢弃全量输入。

    #470 前半场:``include_series=False``(run 未声明用户因子)时跳过
    ``others`` / ``prices`` 的构建 —— 这两块是投影的内存大头(~15MB/期,
    全市场 run 556 期 ≈ 8GB),而 screen 对无 ``u_`` 观测的 run 恒返回
    None,根本不消费它们。防御性兜底:当期 ``user`` 桵非空时无论开关如何
    都构建全量投影(漏报通道的最后一道防线),保证 screen 结果与开关
    无关。
    """

    capture_full = include_series or any(
        is_user_factor_name(value.feature_id) for value in item.features
    )
    if not capture_full:
        # user 桶此时必空(any 探测已排除),others/prices 的构建整体跳过
        return {
            "decision_at": item.decision_at,
            "user": {},
            "others": {},
            "prices": {},
            # issue #463:候选池 symbol→market(_release_end_closes 的期末价
            # 读取需要;候选池内 symbol 唯一,跨期由消费方首见优先合并)。
            "markets": {
                candidate.symbol: candidate.market for candidate in item.candidates
            },
        }
    user: dict[str, dict[str, tuple[float, datetime]]] = {}
    others: dict[str, dict[str, tuple[float, datetime]]] = {}
    for value in item.features:
        raw = value.value
        if raw is None or not math.isfinite(raw):
            continue
        bucket = user if is_user_factor_name(value.feature_id) else others
        target = bucket.setdefault(value.feature_id, {})
        existing = target.get(value.symbol)
        # 同 key 多条观测取 available_at 最新的值 —— 由上游 _features_by_source
        # 保证唯一,这里只做防御性覆盖。
        if existing is None or value.available_at >= existing[1]:
            target[value.symbol] = (float(raw), value.available_at)
    cleaned_user = {name: {s: v for s, (v, _) in series.items()} for name, series in user.items()}
    cleaned_others = {
        name: {s: v for s, (v, _) in series.items()} for name, series in others.items()
    }
    return {
        "decision_at": item.decision_at,
        "user": cleaned_user,
        "others": cleaned_others,
        "prices": dict(item.prices),
        # issue #463:候选池 symbol→market(_release_end_closes 的期末价
        # 读取需要;候选池内 symbol 唯一,跨期由消费方首见优先合并)。
        "markets": {
            candidate.symbol: candidate.market for candidate in item.candidates
        },
    }


async def _forward_close_series(
    manifest: ResearchRunManifest,
    periods: Sequence[dict[str, Any]],
    release_provider_factory: Any,
    *,
    symbols: set[str] | None = None,
) -> list[dict[str, float]]:
    """每期 forward 锚点 close:下一期决策可见 close;末期为发布区间末。"""
    closes = [dict(period["prices"]) for period in periods[1:]]
    closes.append(
        await _release_end_closes(manifest, periods, release_provider_factory, symbols=symbols)
    )
    return closes


async def _release_end_closes(
    manifest: ResearchRunManifest,
    periods: Sequence[dict[str, Any]],
    release_provider_factory: Any,
    *,
    symbols: set[str] | None = None,
) -> dict[str, float]:
    """bars 主发布区间末(end_date 收盘后)可见的最新 close。"""
    from finboard_backtest.research_run.signal_engine import _bars_release_ref

    release_ref = _bars_release_ref(manifest, release_provider_factory)
    provider = release_provider_factory(release_ref.artifact_id)
    release = provider.release
    as_of = datetime.combine(release.end_date + timedelta(days=1), time(0, 0), tzinfo=UTC)
    markets = _symbol_markets(periods)
    prices: dict[str, float] = {}
    for symbol in sorted(symbols if symbols is not None else _screen_symbols(periods)):
        market = markets.get(symbol)
        if market is None:
            continue
        bars = await provider.fetch_point_in_time_bars(
            _symbol_obj(symbol, market),
            release.period,
            release.start_date,
            release.end_date,
            decision_at=as_of,
            adjust=release.adjustment,
        )
        if bars:
            prices[symbol] = float(bars[-1].bar.close)
    return prices


def _screen_symbols(periods: Sequence[dict[str, Any]]) -> set[str]:
    """出现 user 因子观测的标的集合(screen 只关心这些)。

    投影的 ``user`` 桶只含有限值观测(#463:与逐 value 校验
    ``is_user_factor_name ∧ 非空 ∧ isfinite`` 的旧口径等值)。
    """
    symbols: set[str] = set()
    for period in periods:
        for series in period["user"].values():
            symbols.update(series)
    return symbols


def _symbol_markets(periods: Sequence[dict[str, Any]]) -> dict[str, str]:
    """从各期投影候选池收集 symbol → market 映射(首见优先,跨期一致)。"""
    markets: dict[str, str] = {}
    for period in periods:
        for symbol, market in period["markets"].items():
            markets.setdefault(symbol, market)
    return markets


def _symbol_obj(symbol: str, market: str) -> Any:
    from finboard_backtest.research_run.frozen_loader import _market_from_value
    from finboard_shared.models import Symbol

    return Symbol(code=symbol, market=_market_from_value(market))


def _forward_returns(
    periods: Sequence[dict[str, Any]],
    forward_closes: Sequence[dict[str, float]],
) -> list[dict[str, float]]:
    """每期 forward return = forward close / 当期可见 close - 1。"""
    returns: list[dict[str, float]] = []
    for period, forward in zip(periods, forward_closes, strict=True):
        current = period["prices"]
        period_returns: dict[str, float] = {}
        for symbol, base_price in current.items():
            forward_price = forward.get(symbol)
            if forward_price and forward_price > 0 and base_price > 0:
                period_returns[symbol] = forward_price / base_price - 1.0
        returns.append(period_returns)
    return returns


# --------------------------------------------------------------------------- #
# 单因子指标
# --------------------------------------------------------------------------- #


def _screen_one_factor(
    factor: str,
    periods: Sequence[dict[str, Any]],
    forward_returns: Sequence[dict[str, float]],
    issues: list[str],
) -> dict[str, Any]:
    ic_series: list[float] = []
    quantile_buckets: list[list[float | None]] = []
    top_members: list[frozenset[str]] = []
    corr_by_baseline: dict[str, list[float]] = {}

    for period, returns in zip(periods, forward_returns, strict=True):
        scores = period["user"].get(factor, {})
        ic = _spearman(scores, returns)
        if ic is not None:
            ic_series.append(ic)
        buckets, members = _quantile_split(scores)
        quantile_buckets.append(
            [_mean([returns[s] for s in bucket if s in returns]) for bucket in buckets]
        )
        top_members.append(members)
        for name, series in period["others"].items():
            corr = _spearman(scores, series)
            if corr is not None:
                corr_by_baseline.setdefault(name, []).append(corr)

    average_turnover: float | None = None
    if len(top_members) >= _MIN_PERIODS:
        rates = [
            _membership_turnover(prev, curr)
            for prev, curr in pairwise(top_members)
            if prev and curr
        ]
        if rates:
            average_turnover = _mean(rates)

    quantile_returns: list[dict[str, Any]] = []
    for index in range(QUANTILES):
        values = [bucket[index] for bucket in quantile_buckets if bucket[index] is not None]
        quantile_returns.append(
            {
                "quantile": index + 1,
                "average_return": _mean(values),
                "n_periods": len(values),
            }
        )

    rank_ic: float | None = None
    rank_ic_ir: float | None = None
    if ic_series:
        rank_ic = _mean(ic_series)
        if rank_ic is not None and len(ic_series) >= _MIN_PERIODS:
            std = float(np.std(ic_series))
            rank_ic_ir = rank_ic / std if std > 1e-12 else 0.0

    for baseline in sorted(corr_by_baseline):
        if not corr_by_baseline[baseline]:
            issues.append(f"{factor} 与 {baseline} 的相关样本不足(<{_MIN_OVERLAP} 共同标的)")

    return {
        "origin": "user_defined",
        "n_periods": len(periods),
        "rank_ic": rank_ic,
        "rank_ic_ir": rank_ic_ir,
        "ic_sample_count": len(ic_series),
        "quantile_returns": quantile_returns,
        "average_turnover": average_turnover,
        "correlation": {name: _mean(values) for name, values in sorted(corr_by_baseline.items())},
    }


def _quantile_split(
    scores: Mapping[str, float],
) -> tuple[list[list[str]], frozenset[str]]:
    """按因子值升序分 QUANTILES 桶(前 r 桶各多 1 个,array_split 语义)。

    返回 (逐桶 symbol 列表, 最高值桶成员)。
    """
    ordered = sorted(
        ((symbol, value) for symbol, value in scores.items() if math.isfinite(value)),
        key=lambda item: item[1],
    )
    total = len(ordered)
    if total == 0:
        return [[] for _ in range(QUANTILES)], frozenset()
    base, extra = divmod(total, QUANTILES)
    buckets: list[list[str]] = []
    offset = 0
    for index in range(QUANTILES):
        size = base + (1 if index < extra else 0)
        buckets.append([symbol for symbol, _ in ordered[offset : offset + size]])
        offset += size
    return buckets, frozenset(buckets[-1])


def _membership_turnover(previous: frozenset[str], current: frozenset[str]) -> float:
    union = previous | current
    if not union:
        return 0.0
    return len(current - previous) / len(union)


def _spearman(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float | None:
    """spearman 秩相关(共同标的,均值秩 + pearson;与 factors.analysis 同法)。"""
    common = sorted(set(left) & set(right))
    if len(common) < _MIN_OVERLAP:
        return None
    lv = np.array([left[s] for s in common], dtype=float)
    rv = np.array([right[s] for s in common], dtype=float)
    mask = np.isfinite(lv) & np.isfinite(rv)
    if int(mask.sum()) < _MIN_OVERLAP:
        return None
    lr = _average_rank(lv[mask])
    rr = _average_rank(rv[mask])
    lc = lr - lr.mean()
    rc = rr - rr.mean()
    denom = float(np.sqrt((lc**2).sum() * (rc**2).sum()))
    if denom < 1e-12:
        return None
    return float((lc * rc).sum() / denom)


def _average_rank(values: np.ndarray) -> np.ndarray:
    """并列取平均秩(tie-aware)。"""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=float)
    sorted_values = values[order]
    i = 0
    while i < len(sorted_values):
        j = i
        while j + 1 < len(sorted_values) and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _mean(values: Sequence[float | None]) -> float | None:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite:
        return None
    return float(sum(finite) / len(finite))


def _finite_weight(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


__all__ = [
    "QUANTILES",
    "build_factor_screen",
    "build_factor_screen_from_periods",
    "build_strategy_screen",
    "build_strategy_screen_from_periods",
]
