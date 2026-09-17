"""因子执行协议 —— 输出契约(issue #216;#359 增区间协议 v2)。

``factor.compute(ctx)`` 的返回值经 :func:`normalize_result` 收敛为规范形态:
``symbol -> float`` 的截面打分。允许的原始返回:

* :class:`FactorResult`(推荐,显式);
* ``pd.Series``(index=symbol,值为数值);
* ``dict[str, float]``;
* ``pd.DataFrame``(含 ``symbol`` 与 ``score`` 两列)。

未打分(NaN / inf / None)合法,计入 ``nan_ratio``;但出现非数值、空结果、
重复 symbol 或候选池外 symbol 属于输出契约违规(exit 3)。

协议 v2(issue #359):``factor.compute_series(ctx) -> FactorSeries`` 的
返回值经 :func:`normalize_series_result` 收敛 —— 逐决策日截面 ``date ->
symbol -> float | None``;允许的原始返回:

* :class:`FactorSeries`(推荐,显式);
* ``Mapping[date, Mapping[str, float | None]]``;
* ``pd.DataFrame``(含 ``date`` / ``symbol`` / ``score`` 三列的长表)。

未打分(None / NaN)合法,计入逐日 ``nan_ratio``;但日期集与平台传入的
``dates`` 不一致、候选池外 symbol、非数值属于输出契约违规(exit 3)。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


class OutputContractError(Exception):
    """因子输出不符合契约(可读消息,进 /out/error.json)。"""


@dataclass(frozen=True, slots=True)
class FactorResult:
    """截面打分结果(纯函数输出;除 scores 外不允许副作用)。"""

    scores: pd.Series | Mapping[str, float]


def normalize_result(raw: Any) -> pd.Series:
    """把各种合法返回形态收敛为 index=symbol、float64 的 Series。"""
    if isinstance(raw, FactorResult):
        return normalize_result(raw.scores)
    if isinstance(raw, pd.Series):
        series = raw.copy()
        series.index = series.index.astype(str)
        series = series.astype("float64")
        if series.index.duplicated().any():
            dup = series.index[series.index.duplicated()].tolist()[:5]
            raise OutputContractError(f"scores 索引存在重复 symbol: {dup}")
        if series.empty:
            raise OutputContractError("scores 为空(factor 未产出任何标的打分)")
        if series.isna().all():
            raise OutputContractError("scores 全为 NaN,无法构成有效截面")
        return series
    if isinstance(raw, Mapping):
        return normalize_result(
            pd.Series(
                {str(k): float(v) for k, v in raw.items()},
                dtype="float64",
            )
        )
    if isinstance(raw, pd.DataFrame):
        missing = {"symbol", "score"} - set(raw.columns)
        if missing:
            raise OutputContractError(
                f"DataFrame 返回须含 symbol/score 两列,缺 {sorted(missing)}"
            )
        if raw["symbol"].duplicated().any():
            raise OutputContractError("DataFrame.symbol 存在重复值")
        series = pd.Series(
            raw["score"].astype("float64").to_numpy(),
            index=raw["symbol"].astype(str).to_numpy(),
        )
        return normalize_result(series)
    raise OutputContractError(
        f"compute() 返回类型不支持: {type(raw).__name__}"
        f"(允许 FactorResult / pd.Series / dict / DataFrame)"
    )


def result_metrics(scores: pd.Series, *, universe: tuple[str, ...]) -> dict[str, Any]:
    """输出契约指标:coverage / nan_ratio(相对候选池标的数)。"""
    n_universe = len(universe)
    n_valid = int((~scores.isna()).sum())
    n_finite = int(
        scores.dropna().apply(lambda v: math.isfinite(v)).sum()
    )
    return {
        "n_symbols_input": n_universe,
        "n_symbols_scored": len(scores),
        "n_symbols_finite": n_finite,
        "coverage": (n_valid / n_universe) if n_universe else 0.0,
        "nan_ratio": (
            1.0 - (n_valid / len(scores)) if len(scores) else 1.0
        ),
        "n_non_finite": int(len(scores) - n_finite),
    }


@dataclass(frozen=True, slots=True)
class FactorSeries:
    """区间因子输出(协议 v2,issue #359;纯函数输出,无副作用)。

    ``values``:逐决策日截面 ``{date: {symbol: float | None}}`` —— 每个决策
    日对全部候选标的给出打分,None 表示当日未打分(计入 nan_ratio,不属
    契约违规)。契约:value[t] 只许依赖 ``available_at <= t`` 的数据。
    """

    dates: tuple[date, ...]
    values: dict[date, dict[str, float | None]] = field(default_factory=dict)


def normalize_series_result(
    raw: Any,
    *,
    expected_dates: tuple[date, ...],
    universe: tuple[str, ...],
) -> FactorSeries:
    """把 ``compute_series`` 的合法返回收敛为规范 :class:`FactorSeries`。

    校验(违规抛 :class:`OutputContractError`):

    * 日期集与 ``expected_dates`` 完全一致(缺日/多日都不允许 —— 平台按
      窗口决策日消费,因子算不出的日期应显式产出 None 截面);
    * symbol 必须在候选池内;
    * 值须为数值或 None(NaN/inf 合法,计入质量门 nan_ratio)。

    每日截面在 universe 内补全:未出现的 symbol 视为 None(缺测),
    显式给出的值保留原样。
    """
    if isinstance(raw, FactorSeries):
        return normalize_series_result(
            raw.values,
            expected_dates=expected_dates,
            universe=universe,
        )
    if isinstance(raw, Mapping):
        daily: dict[date, dict[str, float | None]] = {}
        for key, cross in raw.items():
            day = _coerce_date(key)
            daily[day] = _normalize_cross_section(cross, universe=universe)
    elif isinstance(raw, pd.DataFrame):
        missing = {"date", "symbol", "score"} - set(raw.columns)
        if missing:
            raise OutputContractError(
                f"DataFrame 返回须含 date/symbol/score 三列,缺 {sorted(missing)}"
            )
        daily = {}
        for row in raw.itertuples(index=False):
            day = _coerce_date(row.date)
            cross = daily.setdefault(day, {})
            symbol = str(row.symbol)
            cross[symbol] = _coerce_value(row.score, symbol=symbol)
        for day, cross in daily.items():
            daily[day] = _normalize_cross_section(cross, universe=universe)
    else:
        raise OutputContractError(
            f"compute_series() 返回类型不支持: {type(raw).__name__}"
            "(允许 FactorSeries / Mapping[date, Mapping] / DataFrame)"
        )
    expected = set(expected_dates)
    produced = set(daily)
    if produced != expected:
        missing_days = sorted(expected - produced)
        extra_days = sorted(produced - expected)
        raise OutputContractError(
            "compute_series 输出日期集与窗口决策日不一致:"
            f"缺 {len(missing_days)} 日(如 {[d.isoformat() for d in missing_days[:3]]}),"
            f"多 {len(extra_days)} 日(如 {[d.isoformat() for d in extra_days[:3]]});"
            f"期望 {len(expected)} 日,产出 {len(produced)} 日"
        )
    return FactorSeries(
        dates=tuple(expected_dates),
        values={day: daily[day] for day in expected_dates},
    )


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise OutputContractError(
        f"compute_series 输出含非法日期键: {value!r}({type(value).__name__})"
    )


def _coerce_value(value: Any, *, symbol: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        raise OutputContractError(
            f"标的 {symbol} 的因子值非数值: {value!r}"
        )
    if isinstance(value, bool | str):
        raise OutputContractError(
            f"标的 {symbol} 的因子值非数值: {value!r}"
        )
    if isinstance(value, int | float | np.floating | np.integer):
        return float(value)
    raise OutputContractError(
        f"标的 {symbol} 的因子值非数值: {value!r}({type(value).__name__})"
    )


def _normalize_cross_section(
    cross: Any, *, universe: tuple[str, ...]
) -> dict[str, float | None]:
    if not isinstance(cross, Mapping):
        raise OutputContractError(
            f"compute_series 的每日截面须为 Mapping[symbol, value],"
            f"收到 {type(cross).__name__}"
        )
    allowed = set(universe)
    normalized: dict[str, float | None] = {}
    for key, value in cross.items():
        symbol = str(key)
        if symbol in normalized:
            raise OutputContractError(
                f"每日截面存在重复 symbol: {symbol}"
            )
        if allowed and symbol not in allowed:
            raise OutputContractError(
                f"scores 含候选池外 symbol {symbol!r}(候选池大小 {len(allowed)})"
            )
        normalized[symbol] = _coerce_value(value, symbol=symbol)
    # universe 内补全:未出现的 symbol 视为当日缺测(None)
    for symbol in universe:
        normalized.setdefault(symbol, None)
    return normalized


def series_metrics(
    series: FactorSeries, *, universe: tuple[str, ...]
) -> dict[str, Any]:
    """区间输出契约指标(协议 v2):逐日截面聚合的 coverage / nan_ratio。

    口径与 :func:`result_metrics` 对齐:nan_ratio = 非有限值(含 NaN/inf)
    占产出格子数;coverage = 有限值格子数 / (决策日数 x 候选池标的数)。
    阈值判定在服务端(``research_sandbox_*``),这里只产原始指标。
    """
    n_days = len(series.dates)
    n_cells = n_days * len(universe)
    n_finite = 0
    n_scored = 0
    for day in series.dates:
        cross = series.values.get(day, {})
        for symbol in universe:
            value = cross.get(symbol)
            if value is None:
                continue
            n_scored += 1
            if math.isfinite(value):
                n_finite += 1
    return {
        "n_dates": n_days,
        "n_symbols_input": len(universe),
        "n_cells": n_cells,
        "n_cells_scored": n_scored,
        "n_cells_finite": n_finite,
        "coverage": (n_finite / n_cells) if n_cells else 0.0,
        "nan_ratio": (
            1.0 - (n_finite / n_scored) if n_scored else 1.0
        ),
    }


@dataclass(frozen=True, slots=True)
class StrategyResult:
    """策略决策输出(纯函数输出;targets = 目标权重,issue #218)。

    ``targets``:index=symbol、value=目标权重(float)。权重语义:

    * 权重和可以小于 1(余下为现金),也可以为 0(空仓观望);
    * 负权重在 long_only 声明下会被服务端组合管线截断为 0(记审计),
      但契约层只拒绝 NaN/inf —— 非 long_only 的研究形态允许负值;
    * 未出现在 targets 中的标的目标权重为 0(含已持有标的 → 清仓)。
    ``meta``:可选诊断信息(进 metrics.json,不参与任何决策语义)。
    """

    targets: pd.Series | Mapping[str, float]
    meta: Mapping[str, Any] | None = None


def normalize_strategy_result(raw: Any) -> tuple[pd.Series, dict[str, Any] | None]:
    """把 ``strategy.decide(ctx)`` 的合法返回收敛为 (权重 Series, meta)。

    允许的原始返回::class:`StrategyResult`、``pd.Series``、``dict[str, float]``。
    与因子打分不同:**允许空 targets**(空 = 本期全现金,合法决策);
    但 NaN/inf/非数值、重复 symbol 属于输出契约违规(exit 3)。
    """
    meta: dict[str, Any] | None = None
    if isinstance(raw, StrategyResult):
        meta = dict(raw.meta) if raw.meta is not None else None
        raw = raw.targets
    if isinstance(raw, pd.Series):
        series = raw.copy()
        series.index = series.index.astype(str)
        series = series.astype("float64")
        if series.index.duplicated().any():
            dup = series.index[series.index.duplicated()].tolist()[:5]
            raise OutputContractError(f"targets 索引存在重复 symbol: {dup}")
    elif isinstance(raw, Mapping):
        series = pd.Series(
            {str(k): float(v) for k, v in raw.items()},
            dtype="float64",
        )
    else:
        raise OutputContractError(
            f"decide() 返回类型不支持: {type(raw).__name__}"
            "(允许 StrategyResult / pd.Series / dict)"
        )
    if not series.empty and not series.apply(math.isfinite).all():
        bad = series.index[~series.apply(math.isfinite)].tolist()[:5]
        raise OutputContractError(f"targets 含 NaN/inf 权重: {bad}")
    return series, meta


def strategy_metrics(
    targets: pd.Series, *, universe: tuple[str, ...], meta: dict[str, Any] | None
) -> dict[str, Any]:
    """策略输出指标(gross/net 敞口、多空计数;相对候选池标的数)。"""
    return {
        "n_symbols_input": len(universe),
        "n_targets": len(targets),
        "n_positive": int((targets > 0).sum()),
        "n_negative": int((targets < 0).sum()),
        "gross_exposure": float(targets.abs().sum()),
        "net_exposure": float(targets.sum()),
        "meta": meta,
    }


__all__ = [
    "FactorResult",
    "FactorSeries",
    "OutputContractError",
    "StrategyResult",
    "normalize_result",
    "normalize_series_result",
    "normalize_strategy_result",
    "result_metrics",
    "series_metrics",
    "strategy_metrics",
]
