"""复合因子组合:加权平均或排名平均。

组合方式在**实验前**声明,禁止运行后按结果人工挑权重。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

EPS = 1e-12


class CombinationMethod(StrEnum):
    WEIGHTED_AVERAGE = "weighted_average"
    RANK_AVERAGE = "rank_average"


@dataclass(frozen=True, slots=True)
class FactorWeight:
    """单个因子在复合评分中的权重和方向。"""

    factor_name: str
    weight: float = 1.0
    direction_override: str | None = None

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(f"因子 {self.factor_name} 权重不能为负: {self.weight}")
        if self.weight > 1.0 + EPS:
            raise ValueError(f"因子 {self.factor_name} 权重不能超过 1.0: {self.weight}")


@dataclass(frozen=True, slots=True)
class CombinationConfig:
    """复合因子组合配置;实验前冻结,不可事后修改。"""

    weights: tuple[FactorWeight, ...]
    method: CombinationMethod = CombinationMethod.WEIGHTED_AVERAGE
    version: str = "v1"

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError("至少需要一个因子权重")
        total = sum(w.weight for w in self.weights)
        if abs(total - 1.0) > 0.02:
            raise ValueError(
                f"因子权重之和应接近 1.0(容差 0.02),当前总和: {total:.4f}"
            )
        names = [w.factor_name for w in self.weights]
        if len(names) != len(set(names)):
            raise ValueError(f"因子权重中有重复: {names}")

    @property
    def factor_names(self) -> tuple[str, ...]:
        return tuple(w.factor_name for w in self.weights)

    @property
    def total_weight(self) -> float:
        return sum(w.weight for w in self.weights)

    @classmethod
    def equal_weight(cls, factor_names: tuple[str, ...]) -> CombinationConfig:
        n = len(factor_names)
        if n == 0:
            raise ValueError("至少需要一个因子")
        w = 1.0 / n
        return cls(weights=tuple(FactorWeight(f, w) for f in factor_names))

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method.value,
            "version": self.version,
            "weights": [
                {
                    "factor": w.factor_name,
                    "weight": round(w.weight, 6),
                    "direction": w.direction_override,
                }
                for w in self.weights
            ],
        }


def combine_scores(
    factor_scores: dict[str, dict[str, float]],
    config: CombinationConfig,
) -> dict[str, float]:
    """将标准化后的因子得分组合为复合得分。

    Args:
        factor_scores: {factor_name: {symbol: standardized_score}}
        config: 组合配置(权重 + 方法)

    Returns:
        {symbol: composite_score} — 得分越高代表预期收益越好
    """
    if not factor_scores:
        return {}

    all_symbols: set[str] = set()
    for fw in config.weights:
        if fw.factor_name in factor_scores:
            all_symbols |= set(factor_scores[fw.factor_name].keys())

    if not all_symbols:
        return {}

    if config.method is CombinationMethod.RANK_AVERAGE:
        return _combine_rank_average(factor_scores, config, all_symbols)
    return _combine_weighted(factor_scores, config, all_symbols)


def _combine_weighted(
    factor_scores: dict[str, dict[str, float]],
    config: CombinationConfig,
    symbols: set[str],
) -> dict[str, float]:
    result: dict[str, float] = {}
    weight_map = {fw.factor_name: fw.weight for fw in config.weights}
    total_weight_used: dict[str, float] = dict.fromkeys(symbols, 0.0)
    for sym in symbols:
        result[sym] = 0.0
    for fw in config.weights:
        scores = factor_scores.get(fw.factor_name, {})
        w = weight_map[fw.factor_name]
        for sym in symbols:
            val = scores.get(sym)
            if val is not None and np.isfinite(val):
                result[sym] += w * val
                total_weight_used[sym] += w
    for sym in symbols:
        if total_weight_used[sym] > EPS:
            result[sym] /= total_weight_used[sym]
        else:
            result[sym] = float("nan")
    return result


def _combine_rank_average(
    factor_scores: dict[str, dict[str, float]],
    config: CombinationConfig,
    symbols: set[str],
) -> dict[str, float]:
    from finboard_backtest.factors.standardize import _rankdata

    weight_map = {fw.factor_name: fw.weight for fw in config.weights}
    accumulated: dict[str, float] = dict.fromkeys(symbols, 0.0)
    total_w: dict[str, float] = dict.fromkeys(symbols, 0.0)
    for fw in config.weights:
        scores = factor_scores.get(fw.factor_name, {})
        if not scores:
            continue
        syms_sorted = sorted(scores.keys())
        vals = np.array(
            [scores[s] if np.isfinite(scores.get(s, float("nan"))) else np.nan for s in syms_sorted],
            dtype=np.float64,
        )
        mask = np.isfinite(vals)
        if mask.sum() < 2:
            continue
        ranks = np.empty_like(vals)
        ranks[~mask] = np.nan
        ranks[mask] = _rankdata(vals[mask])
        n = mask.sum()
        normalized = (ranks - 1.0) / max(n - 1, 1)
        w = weight_map[fw.factor_name]
        for i, sym in enumerate(syms_sorted):
            if mask[i]:
                accumulated[sym] += w * float(normalized[i])
                total_w[sym] += w
    result: dict[str, float] = {}
    for sym in symbols:
        if total_w[sym] > EPS:
            result[sym] = accumulated[sym] / total_w[sym]
        else:
            result[sym] = float("nan")
    return result
