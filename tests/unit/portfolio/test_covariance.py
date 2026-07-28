"""portfolio/covariance.py 的单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from finboard_backtest.portfolio.covariance import (
    CovarianceError,
    estimate_covariance,
    pairwise_aligned_returns,
)


def _make_returns(
    n: int = 100,
    tickers: list[str] | None = None,
    seed: int = 42,
) -> dict[str, np.ndarray]:
    if tickers is None:
        tickers = ["A", "B", "C"]
    rng = np.random.default_rng(seed)
    return {t: rng.standard_normal(n) * 0.01 for t in tickers}


class TestPairwiseAlignedReturns:
    def test_basic_alignment(self) -> None:
        rets = _make_returns(100)
        tickers, matrix = pairwise_aligned_returns(rets)
        assert len(tickers) == 3
        assert matrix.shape == (100, 3)

    def test_different_lengths(self) -> None:
        rets = {
            "A": np.random.default_rng(1).standard_normal(100) * 0.01,
            "B": np.random.default_rng(2).standard_normal(80) * 0.01,
        }
        tickers, matrix = pairwise_aligned_returns(rets, min_overlap=30)
        assert "A" in tickers
        assert "B" in tickers
        assert matrix.shape[0] >= 80

    def test_excludes_short_series(self) -> None:
        rets = {
            "A": np.random.default_rng(1).standard_normal(100) * 0.01,
            "B": np.random.default_rng(2).standard_normal(10) * 0.01,
        }
        tickers, _ = pairwise_aligned_returns(rets, min_overlap=30)
        assert "A" in tickers
        assert "B" not in tickers

    def test_empty_raises(self) -> None:
        with pytest.raises(CovarianceError):
            pairwise_aligned_returns({})

    def test_all_too_short_raises(self) -> None:
        rets = {"A": np.zeros(5)}
        with pytest.raises(CovarianceError):
            pairwise_aligned_returns(rets, min_overlap=30)


class TestEstimateCovariance:
    def test_basic(self) -> None:
        rets = _make_returns(100)
        est = estimate_covariance(rets)
        assert est.n_assets == 3
        assert est.matrix.shape == (3, 3)
        assert 0 <= est.shrinkage <= 1
        assert est.n_observations == 100

    def test_single_asset(self) -> None:
        rets = {"A": np.random.default_rng(1).standard_normal(100) * 0.01}
        est = estimate_covariance(rets)
        assert est.n_assets == 1
        assert est.matrix.shape == (1, 1)
        assert est.shrinkage == pytest.approx(1.0)

    def test_perfectly_correlated(self) -> None:
        base = np.random.default_rng(1).standard_normal(100) * 0.01
        rets = {"A": base, "B": base * 2}
        est = estimate_covariance(rets)
        corr = est.correlation()
        assert corr[0, 1] > 0.99

    def test_independent(self) -> None:
        rng = np.random.default_rng(42)
        rets = {"A": rng.standard_normal(500), "B": rng.standard_normal(500)}
        est = estimate_covariance(rets)
        corr = est.correlation()
        assert abs(corr[0, 1]) < 0.5

    def test_volatilities(self) -> None:
        rets = {
            "A": np.random.default_rng(1).standard_normal(200) * 0.01,
            "B": np.random.default_rng(2).standard_normal(200) * 0.03,
        }
        est = estimate_covariance(rets)
        vols = est.volatilities
        assert vols[1] > vols[0]

    def test_positive_definite(self) -> None:
        rets = _make_returns(100)
        est = estimate_covariance(rets)
        eigenvalues = np.linalg.eigvalsh(est.matrix)
        assert eigenvalues.min() > 0

    def test_shrinkage_increases_with_fewer_obs(self) -> None:
        rng = np.random.default_rng(42)
        rets_few = {t: rng.standard_normal(35) * 0.02 for t in ["A", "B", "C"]}
        rets_many = {t: rng.standard_normal(500) * 0.02 for t in ["A", "B", "C"]}
        est_few = estimate_covariance(rets_few)
        est_many = estimate_covariance(rets_many)
        assert est_few.shrinkage >= est_many.shrinkage

    def test_high_shrinkage_with_min_obs(self) -> None:
        rng = np.random.default_rng(1)
        rets = {t: rng.standard_normal(31) * 0.02 for t in ["A", "B", "C", "D"]}
        est = estimate_covariance(rets, min_observations=30)
        assert est.shrinkage > 0.3

    def test_zero_volatility_asset(self) -> None:
        rng = np.random.default_rng(1)
        rets = {
            "A": rng.standard_normal(100) * 0.01,
            "B": np.zeros(100),
        }
        est = estimate_covariance(rets)
        assert est.n_assets == 2
        assert est.volatilities[1] >= 0.0
