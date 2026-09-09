"""预置因子算子库逐值单测(issue #398,批次 0 基座)。

全部算子以合成数据对照**手算参考值 / 独立 pandas 参照**逐值锁定:

* NaN 纪律:窗口不足 / 窗口内 NaN → NaN(ts);缺测输入 → None、
  不进分母(cs,#380 ``math.isfinite`` 契约);
* 因果性:前 window-1 个位置恒 NaN;截断输入的 prefix 输出与全量
  输出的 prefix 逐值相等(前缀不变性的算子层面);
* 边界:停牌缺行语义 = 行缺失(序列不含该行),非 NaN 污染。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from finboard_backtest.factors.predefined.operators import (
    cs_demean,
    cs_neutralize,
    cs_rank,
    cs_regression_resid,
    cs_scale,
    cs_winsorize,
    cs_zscore,
    rolling_ols_resid,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_decay,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)


def _nan_equal(a: np.ndarray, b: np.ndarray) -> bool:
    """NaN 视为相等的逐值比较(缺测语义一致即一致)。"""
    if a.shape != b.shape:
        return False
    both_nan = np.isnan(a) & np.isnan(b)
    if not np.array_equal(both_nan, np.isnan(a) | np.isnan(b)):
        return False
    return bool(np.allclose(a[~both_nan], b[~both_nan], rtol=1e-12, atol=1e-12))


class TestTimeseriesOperators:
    """ts_* 逐值(手算参考 + pandas 独立参照)。"""

    X = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])

    def test_ts_delay_and_delta_hand_computed(self) -> None:
        x = self.X
        assert _nan_equal(ts_delay(x, 2), np.array([np.nan, np.nan, 1, 2, 3, 4, 5]))
        assert _nan_equal(
            ts_delta(x, 3), np.array([np.nan, np.nan, np.nan, 3, 3, 3, 3])
        )

    def test_ts_mean_matches_pandas(self) -> None:
        x = self.X
        expected = pd.Series(x).rolling(4).mean().to_numpy()
        assert _nan_equal(ts_mean(x, 4), expected)

    def test_ts_std_matches_pandas_sample(self) -> None:
        x = self.X
        expected = pd.Series(x).rolling(4).std(ddof=1).to_numpy()
        assert _nan_equal(ts_std(x, 4), expected)
        # population 口径
        expected0 = pd.Series(x).rolling(4).std(ddof=0).to_numpy()
        assert _nan_equal(ts_std(x, 4, ddof=0), expected0)

    def test_ts_std_free_degree_guard(self) -> None:
        assert _nan_equal(ts_std(self.X, 3, ddof=3), np.full(7, np.nan))

    def test_ts_sum_min_max_hand_computed(self) -> None:
        x = self.X
        assert _nan_equal(ts_sum(x, 3), np.array([np.nan, np.nan, 6, 9, 12, 15, 18]))
        assert _nan_equal(ts_min(x, 3), np.array([np.nan, np.nan, 1, 2, 3, 4, 5]))
        assert _nan_equal(ts_max(x, 3), np.array([np.nan, np.nan, 3, 4, 5, 6, 7]))

    def test_ts_rank_hand_computed(self) -> None:
        x = np.array([5.0, 1.0, 3.0, 3.0, 9.0])
        # i=2: 窗口 [5,1,3],#<=3 = 2 → 2/3;i=3: [1,3,3] #<=3 = 3 → 1;
        # i=4: [3,3,9] #<=9 = 3 → 1
        expected = np.array([np.nan, np.nan, 2 / 3, 1.0, 1.0])
        assert _nan_equal(ts_rank(x, 3), expected)
        # 值域 (0,1]
        ranks = ts_rank(self.X, 4)
        assert float(np.nanmin(ranks)) > 0.0
        assert float(np.nanmax(ranks)) <= 1.0

    def test_ts_decay_linear_weights(self) -> None:
        x = self.X
        # 窗口 [1,2,3],权重 [1,2,3](最近最重):(1+4+9)/6 = 14/6
        assert ts_decay(x, 3)[2] == pytest.approx(14.0 / 6.0)
        # 与 pandas WMA 独立参照逐值对照
        w = np.arange(1.0, 5.0)
        expected = (
            pd.Series(x).rolling(4).apply(lambda v: float(np.dot(v, w) / w.sum()), raw=True).to_numpy()
        )
        assert _nan_equal(ts_decay(x, 4), expected)

    def test_ts_arg_extremes_tie_takes_recent(self) -> None:
        x = np.array([7.0, 1.0, 3.0, 3.0, 2.0])
        # i=2 窗口 [7,1,3]:argmax 相对位置 2(7 最旧,0=当日),argmin 位置 1
        assert ts_argmax(x, 3)[2] == 2.0
        assert ts_argmin(x, 3)[2] == 1.0
        # 并列取最近:i=3 窗口 [1,3,3],最大值并列(位置 1,2)→ 取 0(最近)
        assert ts_argmax(x, 3)[3] == 0.0
        # i=4 窗口 [3,3,2]:argmin 位置 0(值 2 是最近)
        assert ts_argmin(x, 3)[4] == 0.0
        # i=4 窗口 [3,3,2]:argmax 并列(位置 0,1)→ 取最近 = 1
        assert ts_argmax(x, 3)[4] == 1.0

    def test_ts_corr_matches_pandas_and_zero_variance(self) -> None:
        rng = np.random.default_rng(42)
        x = rng.normal(size=60)
        y = 0.5 * x + rng.normal(size=60)
        expected = pd.Series(x).rolling(10).corr(pd.Series(y)).to_numpy()
        assert _nan_equal(ts_corr(x, y, 10), expected)
        # 常数序列零方差 → NaN(fail-visible)
        constant = np.full(60, 2.5)
        assert np.isnan(ts_corr(constant, y, 10)).all()
        assert np.isnan(ts_corr(x, constant, 10)).all()

    def test_ts_cov_matches_pandas(self) -> None:
        rng = np.random.default_rng(7)
        x = rng.normal(size=50)
        y = rng.normal(size=50)
        expected = pd.Series(x).rolling(8).cov(pd.Series(y)).to_numpy()
        assert _nan_equal(ts_cov(x, y, 8), expected)

    def test_rolling_ols_resid_exact_fit_and_reference(self) -> None:
        x = self.X.astype(float)
        # 完全线性 y = 2x + 1 → 残差恒 0(前 window-1 个位置 NaN)
        expected_exact = np.array([np.nan, np.nan, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert _nan_equal(rolling_ols_resid(2 * x + 1, x, 3), expected_exact)
        # pandas 式独立参照:逐窗 OLS
        y = x * 0.5 + np.array([0.0, 1.0, -1.0, 2.0, -2.0, 0.5, -0.5])
        expected: list[float] = [np.nan, np.nan]
        for i in range(2, 7):
            win_x = x[i - 2 : i + 1]
            win_y = y[i - 2 : i + 1]
            slope, intercept = np.polyfit(win_x, win_y, 1)
            expected.append(float(win_y[-1] - (slope * win_x[-1] + intercept)))
        assert _nan_equal(rolling_ols_resid(y, x, 3), np.array(expected))


class TestTimeseriesNanDiscipline:
    """NaN 纪律:窗口内 NaN 传播;窗口不足;因果性 / 前缀不变。"""

    def test_nan_inside_window_propagates(self) -> None:
        x = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
        # i=0/1/2:窗口含 NaN(不足 window 个非 NaN)→ NaN
        out = ts_mean(x, 3)
        assert math.isnan(out[0])
        assert math.isnan(out[1])
        assert math.isnan(out[2])
        # i=3 窗口 [nan,3,4] 仍含 NaN → NaN;i=4 窗口 [3,4,5] → 4
        assert math.isnan(out[3])
        assert out[4] == pytest.approx(4.0)

    def test_short_input_all_nan(self) -> None:
        x = np.array([1.0, 2.0])
        for op in (ts_mean, ts_std, ts_sum, ts_min, ts_max, ts_rank, ts_decay):
            assert np.isnan(op(x, 3)).all(), op.__name__
        assert np.isnan(ts_corr(x, x, 3)).all()
        assert np.isnan(rolling_ols_resid(x, x, 3)).all()

    def test_causal_prefix_invariance(self) -> None:
        """截断输入的前缀输出 == 全量输出的前缀(算子层因果性)。"""
        rng = np.random.default_rng(11)
        x = rng.normal(size=80)
        y = 0.3 * x + rng.normal(size=80)
        single_series_ops: list[Callable[[np.ndarray], np.ndarray]] = [
            lambda v: ts_mean(v, 12),
            lambda v: ts_std(v, 12),
            lambda v: ts_rank(v, 9),
            lambda v: ts_decay(v, 10),
            lambda v: ts_delta(v, 14),
            lambda v: ts_argmax(v, 7),
        ]
        cut = 40
        for op in single_series_ops:
            full = op(x)
            truncated = op(x[:cut])
            assert _nan_equal(full[:cut], truncated), op
        # 双序列算子:同步截断(y 与 x 等长契约)
        pair_ops: list[Callable[[np.ndarray], np.ndarray]] = [
            lambda v: ts_corr(v, y[: len(v)], 15),
            lambda v: ts_cov(v, y[: len(v)], 15),
            lambda v: rolling_ols_resid(y[: len(v)], v, 15),
        ]
        for op in pair_ops:
            full = op(x)
            truncated = op(x[:cut])
            assert _nan_equal(full[:cut], truncated), op
        # 双序列算子:不等长输入 fail-fast
        with pytest.raises(ValueError, match="等长"):
            ts_corr(x, y[:30], 15)

    def test_window_validation(self) -> None:
        with pytest.raises(ValueError, match="window"):
            ts_mean(np.arange(5.0), 0)


class TestCrossSectionOperators:
    """cs_* 逐值:#380 缺测不进分母契约 + 手算参考。"""

    def test_cs_rank_hand_computed_and_missing_denominator(self) -> None:
        values = {"a": 1.0, "b": 2.0, "c": 3.0, "d": None, "e": math.nan}
        out = cs_rank(values)
        assert out["a"] == pytest.approx(1 / 3)
        assert out["b"] == pytest.approx(2 / 3)
        assert out["c"] == pytest.approx(1.0)
        # 缺测输出 None 且分母不含它们(#380)
        assert out["d"] is None
        assert out["e"] is None
        # ties 同分
        out_ties = cs_rank({"a": 1.0, "b": 1.0, "c": 2.0})
        assert out_ties["a"] == pytest.approx(2 / 3)
        assert out_ties["b"] == pytest.approx(2 / 3)
        # 全缺测
        assert cs_rank({"a": None}) == {"a": None}

    def test_cs_zscore_population_and_zero_variance(self) -> None:
        values = {"a": 1.0, "b": 2.0, "c": 3.0}
        out = cs_zscore(values)
        sigma = (2.0 / 3.0) ** 0.5
        assert out["a"] == pytest.approx(-1 / sigma)
        assert out["b"] == pytest.approx(0.0)
        assert out["c"] == pytest.approx(1 / sigma)
        assert all(v is None for v in cs_zscore({"a": 2.0, "b": 2.0}).values())
        # 与 factors.standardize.zscore 同口径(ddof=0)
        from finboard_backtest.factors.standardize import zscore as reference

        ref = reference({"a": 1.0, "b": 2.0, "c": 3.0})
        assert all(out[k] == pytest.approx(ref[k]) for k in ("a", "b", "c"))

    def test_cs_winsorize_clips(self) -> None:
        values = {"a": -10.0, "b": 1.0, "c": 2.0, "d": 3.0, "e": 50.0}
        out = cs_winsorize(values, n_std=1.0)
        # 边界 = finite 全样本(含极值)mean ± n_std*std
        finite = np.array([-10.0, 1.0, 2.0, 3.0, 50.0])
        mean, sigma = finite.mean(), finite.std(ddof=0)
        lower, upper = mean - sigma, mean + sigma
        assert out["a"] == pytest.approx(min(max(-10.0, lower), upper))
        assert out["e"] == pytest.approx(min(max(50.0, lower), upper))
        assert out["b"] == pytest.approx(1.0)
        assert out["d"] is not None
        assert out["d"] == pytest.approx(3.0)
        assert cs_winsorize({"a": None, "b": math.inf})["a"] is None
        with pytest.raises(ValueError, match="n_std"):
            cs_winsorize(values, n_std=0.0)

    def test_cs_demean(self) -> None:
        out = cs_demean({"a": 1.0, "b": 2.0, "c": None})
        assert out["a"] == pytest.approx(-0.5)
        assert out["b"] == pytest.approx(0.5)
        assert out["c"] is None

    def test_cs_neutralize_industry_groups(self) -> None:
        values = {"a": 1.0, "b": 3.0, "c": 10.0, "d": 20.0, "e": None}
        groups = {"a": "bank", "b": "bank", "c": "tech", "d": "tech", "e": "tech"}
        out = cs_neutralize(values, groups)
        # bank 组均值 2:a=-1, b=+1;tech 组均值 15:c=-5, d=+5
        assert out["a"] == pytest.approx(-1.0)
        assert out["b"] == pytest.approx(1.0)
        assert out["c"] == pytest.approx(-5.0)
        assert out["d"] == pytest.approx(5.0)
        assert out["e"] is None
        # 缺组标签归入合成组(不静默丢弃)
        out_missing = cs_neutralize({"a": 4.0, "b": 2.0}, {"a": "bank"})
        assert out_missing["b"] == pytest.approx(0.0)

    def test_cs_regression_resid_against_numpy(self) -> None:
        rng = np.random.default_rng(5)
        symbols = [f"s{i}" for i in range(20)]
        values = {s: float(v) for s, v in zip(symbols, rng.normal(size=20), strict=True)}
        factor = {s: float(v) for s, v in zip(symbols, rng.normal(size=20), strict=True)}
        out = cs_regression_resid(values, factor)
        y = np.array([values[s] for s in symbols])
        x = np.array([factor[s] for s in symbols])
        design = np.column_stack([np.ones(20), x])
        coef = np.linalg.lstsq(design, y, rcond=None)[0]
        expected = y - design @ coef
        for i, symbol in enumerate(symbols):
            assert out[symbol] == pytest.approx(expected[i])
        # 缺测成对删除
        values_missing: dict[str, float | None] = dict(values)
        values_missing["s0"] = None
        out2 = cs_regression_resid(values_missing, factor)
        assert out2["s0"] is None
        assert out2["s1"] is not None

    def test_cs_regression_resid_singular_or_thin(self) -> None:
        values = {"a": 1.0, "b": 2.0}
        factor = {"a": 1.0, "b": 2.0}
        # 有效行数 <= 因子数+1(残差自由度耗尽)→ 全 None
        out = cs_regression_resid(values, factor)
        assert all(v is None for v in out.values())
        # 共线因子列:拟合值/残差唯一(列空间投影),与 numpy 参照一致
        values20 = {f"s{i}": float(i) ** 2 + 1.0 for i in range(6)}
        f1 = {
            s: float(v)
            for s, v in zip(values20, np.arange(6) * 0.7 - 2.0, strict=True)
        }
        f2 = {s: 2.0 * v for s, v in f1.items()}
        out2 = cs_regression_resid(values20, f1, f2)
        y = np.array([values20[f"s{i}"] for i in range(6)])
        design = np.column_stack(
            [np.ones(6), np.arange(6) * 0.7 - 2.0, 2.0 * (np.arange(6) * 0.7 - 2.0)]
        )
        fitted = design @ np.linalg.lstsq(design, y, rcond=None)[0]
        for i in range(6):
            assert out2[f"s{i}"] == pytest.approx(float(y[i] - fitted[i]))

    def test_cs_scale(self) -> None:
        out = cs_scale({"a": 1.0, "b": 3.0, "c": None})
        assert out["a"] == pytest.approx(0.25)
        assert out["b"] == pytest.approx(0.75)
        assert out["c"] is None
        assert all(v is None for v in cs_scale({"a": 0.0}).values())

    def test_cs_operators_inf_treated_as_missing(self) -> None:
        out = cs_rank({"a": 1.0, "b": math.inf})
        assert out["b"] is None
        assert out["a"] == pytest.approx(1.0)
