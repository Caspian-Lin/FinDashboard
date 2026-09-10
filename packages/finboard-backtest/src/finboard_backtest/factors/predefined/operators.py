"""预置因子算子库(issue #398,批次 0 基座)。

202 因子路线图 ≈ 60-70 个原子算子组合;本模块提供第一批纯函数算子,
后续因子批次(#399-#402)的因子实现 = 数据 + 本库算子组合,不再手写
逐值数学。

两组算子,NaN 纪律逐值锁定在单测:

**时序算子**(``ts_*``,1-D ``float64`` ndarray → 1-D):对单标的按行序
(bars 按 ``available_at`` 升序)的因果(trailing)窗口计算——

* 位置 ``i`` 的输出只依赖 ``x[i-window+1 .. i]``(**因果契约**:
  前缀不变性审计兜底检出非因果实现,见 ``research_sandbox.audit``);
* 前 ``window-1`` 个位置输出 NaN(窗口不足);
* 窗口内出现 NaN → 输出 NaN(宁缺毋假:停牌缺行不是 NaN 而是行缺失,
  「窗口 = window 根 bar」语义因此稳定);
* ``ddof`` 仅在有自由度概念的算子暴露,``window - ddof <= 0`` 全列 NaN。

**截面算子**(``cs_*``,``Mapping[str, float | None]`` → 同形):对单一
决策日的截面计算——

* ``None`` / NaN / inf 视为缺测:输出对缺测输入恒 ``None``,且**不参与**
  分母(与信号引擎 ``math.isfinite`` 排名口径一致,#380 契约);
* 输入为空或全缺测时输出全 ``None``(不抛错——缺测是数据常态,
  由序列质量门按覆盖率把关,不在算子层炸构建)。

实现:窗口运算全部经 ``sliding_window_view`` 一次性向量化(逐窗口
two-pass,数值稳健,无 Python 级逐窗循环)。纯离线研究域,不连 broker
不下单。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

CrossSection = Mapping[str, float | None]

__all__ = [
    "CrossSection",
    "cs_demean",
    "cs_neutralize",
    "cs_rank",
    "cs_regression_resid",
    "cs_scale",
    "cs_winsorize",
    "cs_zscore",
    "rolling_ols_resid",
    "ts_argmax",
    "ts_argmin",
    "ts_corr",
    "ts_cov",
    "ts_decay",
    "ts_delay",
    "ts_delta",
    "ts_downside_std",
    "ts_ema",
    "ts_kurt",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_skew",
    "ts_std",
    "ts_sum",
]


# --------------------------------------------------------------------- #
# 时序算子(因果 trailing 窗口;1-D float64)
# --------------------------------------------------------------------- #


def _check_window(window: int) -> None:
    if window < 1:
        raise ValueError(f"window 须 >= 1,收到 {window}")


def _windows(x: np.ndarray, window: int) -> np.ndarray:
    """trailing 窗口视图 ``(n-window+1, window)``,第 k 行 = x[k..k+window-1]。"""
    return sliding_window_view(x, window)


def _window_mask(x: np.ndarray, window: int) -> np.ndarray:
    """逐窗口「无 NaN」掩码;窗口内有 NaN → False(严格传播)。"""
    return ~sliding_window_view(np.isnan(x), window).any(axis=1)


def ts_delay(x: np.ndarray, window: int) -> np.ndarray:
    """``x[i - window]``;前 window 个位置 NaN(位移算子,无窗口内 NaN 传播)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n > window:
        out[window:] = x[: n - window]
    return out


def ts_delta(x: np.ndarray, window: int) -> np.ndarray:
    """``x[i] - x[i-window]``;前 window 个位置 NaN。"""
    _check_window(window)
    result: np.ndarray = x - ts_delay(x, window)
    return result


def ts_sum(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 求和(窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    out[window - 1 :] = np.where(_window_mask(x, window), _windows(x, window).sum(axis=1), np.nan)
    return out


def ts_mean(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 均值(窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    out[window - 1 :] = np.where(
        _window_mask(x, window), _windows(x, window).mean(axis=1), np.nan
    )
    return out


def ts_std(x: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """trailing window 标准差(默认样本口径 ddof=1;窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window or window - ddof <= 0:
        return out
    out[window - 1 :] = np.where(
        _window_mask(x, window),
        _windows(x, window).std(axis=1, ddof=ddof),
        np.nan,
    )
    return out


def ts_min(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 最小值(窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    out[window - 1 :] = np.where(_window_mask(x, window), _windows(x, window).min(axis=1), np.nan)
    return out


def ts_max(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 最大值(窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    out[window - 1 :] = np.where(_window_mask(x, window), _windows(x, window).max(axis=1), np.nan)
    return out


def _arg_extreme(
    x: np.ndarray,
    window: int,
    pick: Callable[..., np.ndarray],
) -> np.ndarray:
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    windows = _windows(x, window)
    # 反转窗口取 arg 最值(axis=1 逐行):反转后下标 j 直接是相对位置
    # (j=0 为最近/当日,window-1 为最旧;并列取最近 = 反转后首个命中)。
    positions = pick(windows[:, ::-1], axis=1)
    out[window - 1 :] = np.where(_window_mask(x, window), positions.astype(np.float64), np.nan)
    return out


def ts_argmax(x: np.ndarray, window: int) -> np.ndarray:
    """最大值在 trailing window 内的相对位置(0=当日,window-1=最旧;并列取最近)。

    窗口内有 NaN 或不足 → NaN。
    """
    return _arg_extreme(x, window, np.argmax)


def ts_argmin(x: np.ndarray, window: int) -> np.ndarray:
    """最小值在 trailing window 内的相对位置(0=当日,window-1=最旧;并列取最近)。"""
    return _arg_extreme(x, window, np.argmin)


def ts_rank(x: np.ndarray, window: int) -> np.ndarray:
    """当日值在 trailing window 内的百分位 ``(# <= x[i]) / window``,值域 (0, 1]。

    并列值同分;窗口内有 NaN 或不足 → NaN。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    windows = _windows(x, window)
    counts = (windows <= windows[:, -1:]).sum(axis=1)
    out[window - 1 :] = np.where(
        _window_mask(x, window), counts.astype(np.float64) / float(window), np.nan
    )
    return out


def ts_decay(x: np.ndarray, window: int) -> np.ndarray:
    """线性衰减加权均值:权重 ``window, ..., 1``(最近一日最重,WMA)。

    窗口内有 NaN 或不足 → NaN。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    weights = np.arange(1.0, float(window) + 1.0)  # 行序 0=最旧 → 最新权重最大
    out[window - 1 :] = np.where(
        _window_mask(x, window),
        _windows(x, window) @ weights / weights.sum(),
        np.nan,
    )
    return out


def ts_ema(x: np.ndarray, span: int) -> np.ndarray:
    """指数移动平均(``adjust=False`` 语义:首有效值种子,``alpha = 2/(span+1)``)。

    NaN 行为(与 ts_* 严格传播不同,EMA 是递推算子,纪律单独定义):

    * NaN 位置输出 NaN(缺测可见),但递推**状态保持**——下一个有效值
      从上一有效状态续算(等价 pandas ``ewm(span, adjust=False,
      ignore_na=True)`` 在有效位置上的取值);
    * 首个有效值之前(或输入空)全 NaN。
    """
    if span < 1:
        raise ValueError(f"span 须 >= 1,收到 {span}")
    alpha = 2.0 / (float(span) + 1.0)
    n = x.size
    out = np.full(n, np.nan)
    state = math.nan
    for i in range(n):
        xi = float(x[i])
        if math.isnan(xi):
            continue
        state = xi if math.isnan(state) else alpha * xi + (1.0 - alpha) * state
        out[i] = state
    return out


def ts_skew(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 偏度(修正 Fisher-Pearson,pandas ``rolling.skew`` 同式)。

    ``window < 3`` 或窗口内有 NaN → NaN;窗口内常数(m2=0)→ NaN。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window or window < 3:
        return out
    w = _windows(x, window)
    centered = w - w.mean(axis=1, keepdims=True)
    m2 = (centered * centered).mean(axis=1)
    m3 = (centered * centered * centered).mean(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        g1 = m3 / m2**1.5
    adjustment = np.sqrt(window * (window - 1.0)) / (window - 2.0)
    out[window - 1 :] = np.where(
        _window_mask(x, window), g1 * adjustment, np.nan
    )
    return out


def ts_kurt(x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 超额峰度(样本口径,pandas ``rolling.kurt`` 同式)。

    ``window < 4`` 或窗口内有 NaN → NaN;窗口内常数(m2=0)→ NaN。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window or window < 4:
        return out
    w = _windows(x, window)
    centered = w - w.mean(axis=1, keepdims=True)
    m2 = (centered * centered).mean(axis=1)
    m4 = (centered**4).mean(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = m4 / (m2 * m2)
    term1 = ((window - 1.0) * (window + 1.0)) / ((window - 2.0) * (window - 3.0))
    term2 = (3.0 * (window - 1.0) ** 2) / ((window - 2.0) * (window - 3.0))
    out[window - 1 :] = np.where(
        _window_mask(x, window), term1 * ratio - term2, np.nan
    )
    return out


def ts_downside_std(x: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """trailing window 下行波动:仅统计窗口内**严格为负**元素的样本标准差。

    负值个数 ``<= ddof`` → NaN;窗口内有 NaN → NaN(严格传播);
    非负元素计 0 参与均值口径(下行偏差惯例)。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    w = _windows(x, window)
    negative = w < 0.0
    counts = negative.sum(axis=1)
    wn = np.where(negative, w, 0.0)
    s1 = wn.sum(axis=1)
    s2 = (wn * wn).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        k = counts.astype(np.float64)
        variance = (s2 - s1 * s1 / k) / (k - ddof)
    out[window - 1 :] = np.where(
        _window_mask(x, window) & (counts > ddof),
        np.sqrt(np.maximum(variance, 0.0)),
        np.nan,
    )
    return out


def _pair_windows(
    x: np.ndarray, y: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """成对有效掩码 + 两序列的窗口视图(任一序列窗口内 NaN → 无效)。"""
    if x.size != y.size:
        raise ValueError(
            f"双序列算子要求等长输入(逐行对齐),收到 {x.size} vs {y.size}"
        )
    return (
        _windows(x, window),
        _windows(y, window),
        _window_mask(x, window) & _window_mask(y, window),
    )


def ts_corr(x: np.ndarray, y: np.ndarray, window: int) -> np.ndarray:
    """trailing window Pearson 相关系数(逐窗口 two-pass)。

    窗口内有 NaN(任一序列)→ NaN;窗口内 x 或 y 常数(零方差)→ NaN。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    wx, wy, valid = _pair_windows(x, y, window)
    xm = wx - wx.mean(axis=1, keepdims=True)
    ym = wy - wy.mean(axis=1, keepdims=True)
    cov = (xm * ym).sum(axis=1)
    denom = np.sqrt((xm * xm).sum(axis=1) * (ym * ym).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        rho = np.where(denom > 0.0, cov / denom, np.nan)
    out[window - 1 :] = np.where(valid, rho, np.nan)
    return out


def ts_cov(x: np.ndarray, y: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """trailing window 协方差(默认样本口径;窗口内有 NaN → NaN)。"""
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window or window - ddof <= 0:
        return out
    wx, wy, valid = _pair_windows(x, y, window)
    xm = wx - wx.mean(axis=1, keepdims=True)
    ym = wy - wy.mean(axis=1, keepdims=True)
    cov = (xm * ym).sum(axis=1) / float(window - ddof)
    out[window - 1 :] = np.where(valid, cov, np.nan)
    return out


def rolling_ols_resid(y: np.ndarray, x: np.ndarray, window: int) -> np.ndarray:
    """trailing window 一元 OLS(带截距)残差 ``y - (a + b*x)``。

    窗口内有 NaN(任一序列)或 x 在窗口内常数(斜率奇异)→ NaN。
    因子语义:价格对基准(指数 / 行业均值)滚动回归的**特质残差**
    (去 beta 暴露),Alpha101 与 Residual 因子族的原子件。
    """
    _check_window(window)
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    wy, wx, valid = _pair_windows(y, x, window)
    xm = wx - wx.mean(axis=1, keepdims=True)
    ym = wy - wy.mean(axis=1, keepdims=True)
    sxx = (xm * xm).sum(axis=1)
    sxy = (xm * ym).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = np.where(sxx > 0.0, sxy / np.where(sxx > 0.0, sxx, 1.0), np.nan)
    intercept = wy.mean(axis=1) - slope * wx.mean(axis=1)
    resid = wy[:, -1] - intercept - slope * wx[:, -1]
    out[window - 1 :] = np.where(valid, resid, np.nan)
    return out


# --------------------------------------------------------------------- #
# 截面算子(单决策日截面;None/NaN/inf = 缺测,不进分母,#380 口径)
# --------------------------------------------------------------------- #


def _is_finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _finite_value(value: float | None) -> float | None:
    """缺测(None/NaN/inf)→ None;有限值 → float(mypy 收窄点)。"""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _finite_pairs(values: CrossSection) -> list[tuple[str, float]]:
    pairs: list[tuple[str, float]] = []
    for symbol, value in values.items():
        finite = _finite_value(value)
        if finite is not None:
            pairs.append((symbol, finite))
    return pairs


def cs_rank(values: CrossSection) -> dict[str, float | None]:
    """截面百分位排名 ``(# finite <= x) / n_finite``,值域 (0, 1]。

    缺测输入输出 ``None`` 且不参与分母(#380 契约:NaN 不进排名对、
    不进分母;``math.isfinite`` 过滤口径)。
    """
    pairs = _finite_pairs(values)
    if not pairs:
        return dict.fromkeys(values, None)
    arr = np.array([v for _, v in pairs], dtype=np.float64)
    # side="right":小于等于 x 的元素数(并列同分)。
    counts = np.searchsorted(np.sort(arr), arr, side="right")
    rank_by_symbol = {
        symbol: float(counts[i]) / float(arr.size)
        for i, (symbol, _) in enumerate(pairs)
    }
    return {symbol: rank_by_symbol.get(symbol) for symbol in values}


def cs_zscore(values: CrossSection) -> dict[str, float | None]:
    """截面 z-score(finite 域,population 口径 ddof=0,与
    ``factors.standardize.zscore`` 一致);零方差输出全 ``None``。"""
    pairs = _finite_pairs(values)
    result: dict[str, float | None] = dict.fromkeys(values, None)
    if not pairs:
        return result
    arr = np.array([v for _, v in pairs], dtype=np.float64)
    mean = float(arr.mean())
    sigma = float(arr.std(ddof=0))
    if sigma == 0.0 or not math.isfinite(sigma):
        return result
    for symbol, v in values.items():
        value = _finite_value(v)
        if value is not None:
            result[symbol] = (value - mean) / sigma
    return result


def cs_winsorize(values: CrossSection, n_std: float = 3.0) -> dict[str, float | None]:
    """截面缩尾:clip 至 finite 域 ``mean ± n_std*std``(population 口径)。

    ``n_std <= 0`` 抛 ``ValueError``;缺测保持 ``None``。
    """
    if n_std <= 0:
        raise ValueError(f"n_std 须 > 0,收到 {n_std}")
    pairs = _finite_pairs(values)
    result: dict[str, float | None] = dict.fromkeys(values, None)
    if not pairs:
        return result
    arr = np.array([v for _, v in pairs], dtype=np.float64)
    mean = float(arr.mean())
    sigma = float(arr.std(ddof=0))
    lower = mean - n_std * sigma
    upper = mean + n_std * sigma
    for symbol, v in values.items():
        value = _finite_value(v)
        if value is not None:
            result[symbol] = min(max(value, lower), upper)
    return result


def cs_demean(values: CrossSection) -> dict[str, float | None]:
    """截面去均值(finite 域均值;缺测保持 ``None``)。"""
    pairs = _finite_pairs(values)
    result: dict[str, float | None] = dict.fromkeys(values, None)
    if not pairs:
        return result
    mean = float(np.array([v for _, v in pairs], dtype=np.float64).mean())
    for symbol, v in values.items():
        value = _finite_value(v)
        if value is not None:
            result[symbol] = value - mean
    return result


def cs_neutralize(
    values: CrossSection,
    groups: Mapping[str, str | None],
) -> dict[str, float | None]:
    """分组去均值(行业中性化 v1:组内 de-mean,issue #398)。

    ``groups`` 为 symbol → 组标签(如行业一级代码);缺组标签的标的
    统一归入合成组 ``""``(一起参与去均值,不静默丢弃);缺测保持 ``None``;
    单标的组去均值后为 0。行业分组观测自研究发布
    ``industry_memberships`` 装配(v1 允许显式传入分组序列,见引擎
    ``PredefinedFactorInput.industry_groups``)。
    """
    group_keys = {symbol: groups.get(symbol) or "" for symbol in values}
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for symbol, value in values.items():
        finite = _finite_value(value)
        if finite is None:
            continue
        key = group_keys[symbol]
        totals[key] = totals.get(key, 0.0) + finite
        counts[key] = counts.get(key, 0) + 1
    means = {key: total / counts[key] for key, total in totals.items()}
    result: dict[str, float | None] = dict.fromkeys(values, None)
    for symbol, v in values.items():
        value = _finite_value(v)
        if value is not None:
            result[symbol] = value - means[group_keys[symbol]]
    return result


def cs_regression_resid(
    values: CrossSection,
    *factors: CrossSection,
) -> dict[str, float | None]:
    """截面多元 OLS(带截距)残差:``values`` 对 1..8 个因子列回归取残差。

    成对删除缺测行(任一列缺测的标的不进估计且输出 ``None``);
    有效行数 <= 因子数 + 1(残差自由度耗尽)或拟合出现非有限值 →
    全 ``None``。因子列共线时最小二乘解不唯一但**拟合值 / 残差唯一**
    (列空间投影),直接经 ``lstsq`` 最小范数解产出。
    典型用途:规模中性化(resid against log 市值)。
    """
    if not 1 <= len(factors) <= 8:
        raise ValueError(f"因子列数须在 1..8,收到 {len(factors)}")
    out: dict[str, float | None] = dict.fromkeys(values, None)
    complete = [
        symbol
        for symbol, v in values.items()
        if _finite_value(v) is not None
        and all(_finite_value(f.get(symbol)) is not None for f in factors)
    ]
    if len(complete) <= len(factors) + 1:
        return out
    y_list: list[float] = []
    for s in complete:
        value = _finite_value(values[s])
        if value is not None:
            y_list.append(value)
    y = np.array(y_list, dtype=np.float64)
    columns: list[list[float]] = []
    for f in factors:
        column: list[float] = []
        for s in complete:
            value = _finite_value(f[s])
            if value is not None:
                column.append(value)
        columns.append(column)
    design = np.column_stack(
        [
            np.ones(len(complete), dtype=np.float64),
            *[np.array(col, dtype=np.float64) for col in columns],
        ]
    )
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ coef
    if not np.all(np.isfinite(fitted)) or not np.all(np.isfinite(coef)):
        return out
    for i, symbol in enumerate(complete):
        out[symbol] = float(y[i] - fitted[i])
    return out


def cs_scale(values: CrossSection) -> dict[str, float | None]:
    """``x / Σ|x|``(finite 域;Alpha101 常用归一)。``Σ|x| = 0`` → 全 ``None``。"""
    pairs = _finite_pairs(values)
    result: dict[str, float | None] = dict.fromkeys(values, None)
    if not pairs:
        return result
    total = sum(abs(v) for _, v in pairs)
    if total == 0.0:
        return result
    for symbol, v in values.items():
        value = _finite_value(v)
        if value is not None:
            result[symbol] = value / total
    return result
