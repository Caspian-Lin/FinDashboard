"""横截面标准化工具。

所有函数接受 ``dict[str, float]`` (symbol → raw_value) 并返回同结构的标准化值。
缺失值(NaN)被排除在统计量计算之外但保留在输出中。

设计原则:
- 确定性:相同输入永远产生相同输出(不依赖 dict 遍历顺序之外的随机性)。
- 缺失安全:NaN 不污染统计量,不影响其他标的的标准化结果。
- 极值鲁棒:winsorize 和 rank_normalize 对极端值不敏感。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import numpy as np

_EPS = 1e-12


def winsorize(
    values: dict[str, float],
    *,
    lower_pct: float = 0.01,
    upper_pct: float = 0.99,
) -> dict[str, float]:
    """按百分位截尾。

    超出 ``[lower_pct, upper_pct]`` 分位数的值被 clip 到边界。
    """
    if not values:
        return {}
    arr = np.array(list(values.values()), dtype=np.float64)
    mask = np.isfinite(arr)
    finite = arr[mask]
    if finite.size < 2:
        return dict(values)
    lo = float(np.quantile(finite, lower_pct))
    hi = float(np.quantile(finite, upper_pct))
    if hi <= lo:
        return dict(values)
    return {k: float(np.clip(v, lo, hi)) for k, v in values.items()}


def zscore(values: dict[str, float]) -> dict[str, float]:
    """Z-score 标准化: (x - mean) / std。

    缺失值(NaN)保留为 NaN,不参与均值/标准差计算。
    """
    if not values:
        return {}
    arr = np.array(list(values.values()), dtype=np.float64)
    mask = np.isfinite(arr)
    finite = arr[mask]
    if finite.size < 2:
        return {k: 0.0 if np.isfinite(v) else float("nan") for k, v in values.items()}
    mu = float(finite.mean())
    sigma = float(finite.std(ddof=0))
    if sigma < _EPS:
        return {k: 0.0 if np.isfinite(v) else float("nan") for k, v in values.items()}
    return {k: (v - mu) / sigma if np.isfinite(v) else float("nan") for k, v in values.items()}


def rank_normalize(
    values: dict[str, float],
    *,
    method: str = "average",
) -> dict[str, float]:
    """排名标准化:将值映射到 [0, 1] 均匀分布,再做 z-score。

    比原始 z-score 更鲁棒,不受极端值影响。
    """
    if not values:
        return {}
    arr = np.array(list(values.values()), dtype=np.float64)
    mask = np.isfinite(arr)
    n = mask.sum()
    if n < 2:
        return {k: 0.0 if np.isfinite(v) else float("nan") for k, v in values.items()}
    ranks_raw = np.empty_like(arr)
    ranks_raw[~mask] = np.nan
    ranks_raw[mask] = _rankdata(arr[mask], method=method)
    uniform = (ranks_raw - 1.0) / max(n - 1, 1)
    mu = float(np.nanmean(uniform))
    sigma = float(np.nanstd(uniform))
    if sigma < _EPS:
        return {k: 0.0 if np.isfinite(v) else float("nan") for k, v in values.items()}
    keys = list(values.keys())
    result = {k: float("nan") for k in keys}
    for i, k in enumerate(keys):
        if mask[i]:
            result[k] = float((uniform[i] - mu) / sigma)
    return result


def industry_demean(
    values: dict[str, float],
    industry_map: dict[str, str],
) -> dict[str, float]:
    """行业中性化:减去所属行业的中位数。

    缺失行业的标的减去全局中位数。
    """
    if not values:
        return {}
    by_industry: dict[str, list[float]] = defaultdict(list)
    all_vals: list[float] = []
    for sym, val in values.items():
        if not np.isfinite(val):
            continue
        ind = industry_map.get(sym, "__unknown__")
        by_industry[ind].append(val)
        all_vals.append(val)
    if not all_vals:
        return dict(values)
    global_median = float(np.median(all_vals))
    ind_medians: dict[str, float] = {}
    for ind, vals in by_industry.items():
        if len(vals) >= 3:
            ind_medians[ind] = float(np.median(vals))
        else:
            ind_medians[ind] = global_median
    return {
        sym: val - ind_medians.get(industry_map.get(sym, "__unknown__"), global_median)
        if np.isfinite(val)
        else float("nan")
        for sym, val in values.items()
    }


def regression_neutralize(
    values: dict[str, float],
    control: dict[str, float],
) -> dict[str, float]:
    """回归中性化:对控制变量做 OLS 回归,返回残差。

    常用于市值中性化:control = log(market_cap)。
    缺失控制变量的标的的原始值保留(不减)。
    """
    symbols = sorted(set(values) & set(control))
    y = np.array([values[s] for s in symbols], dtype=np.float64)
    x = np.array([control[s] for s in symbols], dtype=np.float64)
    y_mask = np.isfinite(y)
    x_mask = np.isfinite(x)
    both = y_mask & x_mask
    if both.sum() < 3:
        return dict(values)
    y_fit = y[both]
    x_fit = x[both]
    x_mat = np.column_stack([np.ones(both.sum()), x_fit])
    coef, *_ = np.linalg.lstsq(x_mat, y_fit, rcond=None)
    result = {}
    for s in values:
        if s in control and np.isfinite(values[s]) and np.isfinite(control[s]):
            predicted = coef[0] + coef[1] * control[s]
            result[s] = float(values[s] - predicted)
        else:
            result[s] = values[s]
    return result


def apply_direction(
    values: dict[str, float],
    direction_sign: float,
) -> dict[str, float]:
    """按因子方向翻转符号:SHORT 因子乘 -1 使高分始终代表"好"。"""
    if direction_sign >= 0:
        return dict(values)
    return {k: -v if np.isfinite(v) else float("nan") for k, v in values.items()}


def fill_missing(
    values: dict[str, float],
    strategy: str,
) -> dict[str, float]:
    """按策略填充缺失值。"""
    if strategy == "exclude":
        return dict(values)
    finite = [v for v in values.values() if np.isfinite(v)]
    if not finite:
        return dict(values)
    if strategy == "fill_median":
        fill_val = float(np.median(finite))
    elif strategy == "fill_worst":
        fill_val = float(np.min(finite))
    else:
        return dict(values)
    return {k: fill_val if not np.isfinite(v) else v for k, v in values.items()}


def _rankdata(arr: np.ndarray, method: str = "average") -> np.ndarray:
    """纯 numpy rankdata,避免依赖 scipy。"""
    sorter = np.argsort(arr, kind="mergesort")
    inv = np.empty(sorter.size, dtype=np.intp)
    inv[sorter] = np.arange(sorter.size)
    arr_sorted = arr[sorter]
    obs = np.r_[True, arr_sorted[1:] != arr_sorted[:-1]]
    dense = obs.cumsum()[inv]
    count = np.r_[np.nonzero(obs)[0], len(obs)]
    if method == "average":
        return np.asarray(0.5 * (count[dense] + count[dense - 1] + 1), dtype=np.float64)
    elif method == "min":
        return np.asarray(count[dense - 1] + 1.0, dtype=np.float64)
    elif method == "max":
        return np.asarray(count[dense], dtype=np.float64)
    elif method == "ordinal":
        return np.asarray(inv, dtype=np.float64) + 1.0
    return np.asarray(0.5 * (count[dense] + count[dense - 1] + 1), dtype=np.float64)


def standardize_series(
    values: Sequence[float],
    *,
    lower_pct: float = 0.01,
    upper_pct: float = 0.99,
    method: str = "zscore",
) -> np.ndarray:
    """对一维序列做 winsorize → 标准化,返回 numpy 数组。"""
    arr = np.array(values, dtype=np.float64)
    mask = np.isfinite(arr)
    finite = arr[mask]
    if finite.size < 2:
        return arr
    lo = float(np.quantile(finite, lower_pct))
    hi = float(np.quantile(finite, upper_pct))
    if hi > lo:
        arr = np.clip(arr, lo, hi)
    finite = arr[mask]
    mu = finite.mean()
    sigma = finite.std(ddof=0)
    if sigma < _EPS:
        return np.zeros_like(arr)
    if method == "rank":
        ranks = np.empty_like(arr)
        ranks[~mask] = np.nan
        ranks[mask] = _rankdata(arr[mask])
        n = mask.sum()
        uniform = (ranks - 1.0) / max(n - 1, 1)
        mu_u = float(np.nanmean(uniform))
        sigma_u = float(np.nanstd(uniform))
        if sigma_u < _EPS:
            return np.zeros_like(arr)
        return np.asarray((uniform - mu_u) / sigma_u)
    result: np.ndarray = (arr - mu) / sigma
    result[~mask] = np.nan
    return result
