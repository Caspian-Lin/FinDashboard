"""portfolio/attribution.py 的单元测试。"""

from __future__ import annotations

import numpy as np

from finboard_backtest.portfolio.attribution import compute_attribution
from finboard_backtest.portfolio.covariance import CovarianceEstimate


def _cov(tickers: list[str], vols: list[float], corr: float = 0.3) -> CovarianceEstimate:
    n = len(tickers)
    c = np.full((n, n), corr)
    np.fill_diagonal(c, 1.0)
    v = np.array(vols)
    return CovarianceEstimate(
        matrix=np.outer(v, v) * c,
        tickers=tickers,
        shrinkage=0.3,
        n_observations=252,
    )


class TestComputeAttribution:
    def test_empty_weights(self) -> None:
        cov = _cov(["A"], [0.01])
        report = compute_attribution(
            weights_history=[{}],
            returns_by_ticker={"A": np.array([0.01, 0.02])},
            covariance=cov,
        )
        assert report.n_assets == 0

    def test_single_asset(self) -> None:
        cov = _cov(["A"], [0.01])
        rets = np.array([0.01, -0.005, 0.008])
        report = compute_attribution(
            weights_history=[{"A": 1.0}],
            returns_by_ticker={"A": rets},
            covariance=cov,
        )
        assert report.n_assets == 1
        ac = report.asset_contribution("A")
        assert ac is not None
        assert ac.weight == 1.0
        assert ac.risk_contribution == 1.0

    def test_two_assets_sleeve(self) -> None:
        cov = _cov(["A", "B"], [0.01, 0.02], corr=0.3)
        rets_a = np.array([0.01, 0.02, -0.01])
        rets_b = np.array([0.005, -0.01, 0.02])
        report = compute_attribution(
            weights_history=[{"A": 0.50, "B": 0.50}],
            returns_by_ticker={"A": rets_a, "B": rets_b},
            covariance=cov,
            sleeve_map={"A": "equity", "B": "bond"},
        )
        assert report.n_sleeves == 2
        equity = report.sleeve_contribution("equity")
        bond = report.sleeve_contribution("bond")
        assert equity is not None
        assert bond is not None

    def test_turnover_contribution(self) -> None:
        cov = _cov(["A", "B"], [0.01, 0.01])
        report = compute_attribution(
            weights_history=[
                {"A": 0.60, "B": 0.40},
                {"A": 0.40, "B": 0.60},
            ],
            returns_by_ticker={
                "A": np.array([0.01, 0.0]),
                "B": np.array([0.0, 0.01]),
            },
            covariance=cov,
        )
        assert report.total_turnover > 0

    def test_drawdown(self) -> None:
        cov = _cov(["A"], [0.01])
        rets = np.array([0.05, -0.10, -0.05])
        report = compute_attribution(
            weights_history=[{"A": 1.0}],
            returns_by_ticker={"A": rets},
            covariance=cov,
        )
        assert report.max_drawdown < 0

    def test_default_sleeve(self) -> None:
        cov = _cov(["A", "B"], [0.01, 0.01])
        report = compute_attribution(
            weights_history=[{"A": 0.50, "B": 0.50}],
            returns_by_ticker={
                "A": np.array([0.01]),
                "B": np.array([0.01]),
            },
            covariance=cov,
        )
        assert report.n_sleeves == 1
        assert report.sleeve_contribution("default") is not None
