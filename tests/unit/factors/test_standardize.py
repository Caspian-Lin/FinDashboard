"""横截面标准化工具测试。"""

from __future__ import annotations

import math

import numpy as np

from finboard_backtest.factors.standardize import (
    _rankdata,
    apply_direction,
    fill_missing,
    industry_demean,
    rank_normalize,
    regression_neutralize,
    standardize_series,
    winsorize,
    zscore,
)


class TestWinsorize:
    def test_basic_clip(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 100.0}
        result = winsorize(vals, lower_pct=0.2, upper_pct=0.8)
        assert result["e"] < 100.0
        assert result["b"] == 2.0

    def test_no_clip_when_range_ok(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0}
        result = winsorize(vals, lower_pct=0.0, upper_pct=1.0)
        assert result == vals

    def test_empty(self) -> None:
        assert winsorize({}) == {}

    def test_single_value(self) -> None:
        result = winsorize({"a": 5.0})
        assert result == {"a": 5.0}

    def test_nan_preserved(self) -> None:
        vals = {"a": 1.0, "b": float("nan"), "c": 100.0}
        result = winsorize(vals)
        assert math.isnan(result["b"])

    def test_extreme_outlier_clipped(self) -> None:
        vals = {f"s{i}": float(i) for i in range(20)}
        vals["outlier"] = 1e6
        result = winsorize(vals, lower_pct=0.05, upper_pct=0.95)
        assert result["outlier"] < 1e6


class TestZscore:
    def test_basic(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0}
        result = zscore(vals)
        assert result["a"] < 0
        assert abs(result["b"]) < 1e-10
        assert result["c"] > 0

    def test_mean_zero_std_one(self) -> None:
        vals = {f"s{i}": float(i) for i in range(100)}
        result = zscore(vals)
        arr = np.array(list(result.values()))
        assert abs(arr.mean()) < 1e-10
        assert abs(arr.std() - 1.0) < 1e-10

    def test_nan_preserved(self) -> None:
        vals = {"a": 1.0, "b": float("nan"), "c": 3.0}
        result = zscore(vals)
        assert math.isnan(result["b"])

    def test_constant_returns_zero(self) -> None:
        vals = {"a": 5.0, "b": 5.0, "c": 5.0}
        result = zscore(vals)
        assert all(abs(v) < 1e-10 for v in result.values())

    def test_empty(self) -> None:
        assert zscore({}) == {}


class TestRankNormalize:
    def test_basic_uniform(self) -> None:
        vals = {"a": 10.0, "b": 20.0, "c": 30.0, "d": 40.0, "e": 50.0}
        result = rank_normalize(vals)
        assert result["a"] < result["b"] < result["c"] < result["d"] < result["e"]

    def test_mean_zero(self) -> None:
        vals = {f"s{i}": float(i * 2) for i in range(50)}
        result = rank_normalize(vals)
        arr = np.array(list(result.values()))
        assert abs(arr.mean()) < 1e-10

    def test_robust_to_outlier(self) -> None:
        vals = {f"s{i}": float(i) for i in range(20)}
        vals["outlier"] = 1e6
        result = rank_normalize(vals)
        assert abs(result["s0"] - result["outlier"]) < 5.0

    def test_nan_excluded(self) -> None:
        vals = {"a": 1.0, "b": float("nan"), "c": 3.0, "d": 5.0, "e": 7.0}
        result = rank_normalize(vals)
        assert math.isnan(result["b"])
        assert np.isfinite(result["a"])

    def test_ties_average_rank(self) -> None:
        vals = {"a": 1.0, "b": 1.0, "c": 2.0}
        result = rank_normalize(vals)
        assert abs(result["a"] - result["b"]) < 1e-10


class TestIndustryDemean:
    def test_basic(self) -> None:
        vals = {"a": 10.0, "b": 12.0, "c": 20.0, "d": 22.0, "e": 30.0}
        ind = {"a": "tech", "b": "tech", "c": "bank", "d": "bank", "e": "tech"}
        result = industry_demean(vals, ind)
        tech_median = 12.0
        assert abs(result["a"] - (10.0 - tech_median)) < 1e-10
        assert abs(result["b"] - (12.0 - tech_median)) < 1e-10

    def test_small_industry_uses_global(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 100.0}
        ind = {"a": "x", "b": "x", "c": "y", "d": "y"}
        result = industry_demean(vals, ind)
        global_median = 2.5
        assert abs(result["a"] - (1.0 - global_median)) < 1e-10

    def test_missing_industry_uses_global(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0}
        ind = {"a": "x", "b": "x", "c": "x", "d": "x"}
        result = industry_demean(vals, ind)
        global_median = 3.0
        assert abs(result["e"] - (5.0 - global_median)) < 1e-10

    def test_nan_preserved(self) -> None:
        vals = {"a": 1.0, "b": float("nan"), "c": 3.0, "d": 4.0, "e": 5.0}
        ind = {"a": "x", "b": "x", "c": "x", "d": "y", "e": "y"}
        result = industry_demean(vals, ind)
        assert math.isnan(result["b"])


class TestRegressionNeutralize:
    def test_residual_uncorrelated_with_control(self) -> None:
        rng = np.random.default_rng(42)
        control_vals = rng.uniform(10, 20, 100)
        y = 2.0 * control_vals + 1.0 + rng.standard_normal(100) * 0.1
        syms = [f"s{i}" for i in range(100)]
        values = {s: float(y[i]) for i, s in enumerate(syms)}
        control = {s: float(control_vals[i]) for i, s in enumerate(syms)}
        residual = regression_neutralize(values, control)
        res_arr = np.array([residual[s] for s in syms])
        ctrl_arr = np.array([control[s] for s in syms])
        corr = np.corrcoef(res_arr, ctrl_arr)[0, 1]
        assert abs(corr) < 0.2

    def test_missing_control_preserves_value(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}
        control = {"a": 1.0, "b": 2.0, "c": 3.0}
        result = regression_neutralize(vals, control)
        assert "d" in result

    def test_too_few_returns_original(self) -> None:
        vals = {"a": 1.0, "b": 2.0}
        control = {"a": 1.0, "b": 2.0}
        result = regression_neutralize(vals, control)
        assert result == vals


class TestApplyDirection:
    def test_long_unchanged(self) -> None:
        vals = {"a": 1.0, "b": -2.0}
        result = apply_direction(vals, 1.0)
        assert result == vals

    def test_short_flipped(self) -> None:
        vals = {"a": 1.0, "b": -2.0}
        result = apply_direction(vals, -1.0)
        assert result == {"a": -1.0, "b": 2.0}

    def test_nan_preserved(self) -> None:
        vals = {"a": float("nan")}
        result = apply_direction(vals, -1.0)
        assert math.isnan(result["a"])


class TestFillMissing:
    def test_exclude(self) -> None:
        vals = {"a": 1.0, "b": float("nan")}
        result = fill_missing(vals, "exclude")
        assert math.isnan(result["b"])

    def test_fill_median(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": float("nan")}
        result = fill_missing(vals, "fill_median")
        assert result["d"] == 2.0

    def test_fill_worst(self) -> None:
        vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": float("nan")}
        result = fill_missing(vals, "fill_worst")
        assert result["d"] == 1.0


class TestRankdata:
    def test_no_ties(self) -> None:
        arr = np.array([3.0, 1.0, 2.0])
        ranks = _rankdata(arr)
        assert ranks.tolist() == [3.0, 1.0, 2.0]

    def test_ties_average(self) -> None:
        arr = np.array([1.0, 1.0, 2.0])
        ranks = _rankdata(arr, method="average")
        assert ranks[0] == ranks[1] == 1.5
        assert ranks[2] == 3.0

    def test_ties_min(self) -> None:
        arr = np.array([1.0, 1.0, 2.0])
        ranks = _rankdata(arr, method="min")
        assert ranks[0] == ranks[1] == 1.0


class TestStandardizeSeries:
    def test_basic_zscore(self) -> None:
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = standardize_series(vals, method="zscore")
        assert abs(np.mean(result)) < 1e-10
        assert abs(np.std(result) - 1.0) < 1e-10

    def test_basic_rank(self) -> None:
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = standardize_series(vals, method="rank")
        assert result[0] < result[-1]

    def test_winsorize_applied(self) -> None:
        vals = [*list(range(100)), 10000]
        result = standardize_series(vals, lower_pct=0.01, upper_pct=0.99)
        assert np.max(result) < 100.0
