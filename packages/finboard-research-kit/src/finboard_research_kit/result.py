"""因子执行协议 v1 —— 输出契约(issue #216)。

``factor.compute(ctx)`` 的返回值经 :func:`normalize_result` 收敛为规范形态:
``symbol -> float`` 的截面打分。允许的原始返回:

* :class:`FactorResult`(推荐,显式);
* ``pd.Series``(index=symbol,值为数值);
* ``dict[str, float]``;
* ``pd.DataFrame``(含 ``symbol`` 与 ``score`` 两列)。

未打分(NaN / inf / None)合法,计入 ``nan_ratio``;但出现非数值、空结果、
重复 symbol 或候选池外 symbol 属于输出契约违规(exit 3)。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    "OutputContractError",
    "StrategyResult",
    "normalize_result",
    "normalize_strategy_result",
    "result_metrics",
    "strategy_metrics",
]
