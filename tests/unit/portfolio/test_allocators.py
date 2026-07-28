"""portfolio/allocators.py 的单元测试。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from finboard_backtest.portfolio.allocators import (
    AllocationError,
    EqualWeightAllocator,
    ErcAllocator,
    InverseVolatilityAllocator,
    make_allocator,
)
from finboard_backtest.portfolio.contracts import (
    PortfolioConstraints,
    Signal,
)
from finboard_backtest.portfolio.covariance import (
    CovarianceEstimate,
)


def _signals(codes: list[str], ts: date | None = None) -> list[Signal]:
    ts = ts or date(2024, 6, 28)
    return [Signal(symbol=c, score=1.0, timestamp=ts, strategy_id="test") for c in codes]


def _cov_estimate(
    tickers: list[str],
    vols: list[float],
    corr: float = 0.3,
    n_obs: int = 252,
) -> CovarianceEstimate:
    n = len(tickers)
    corr_mat = np.full((n, n), corr)
    np.fill_diagonal(corr_mat, 1.0)
    vol_arr = np.array(vols)
    cov = np.outer(vol_arr, vol_arr) * corr_mat
    return CovarianceEstimate(
        matrix=cov,
        tickers=tickers,
        shrinkage=0.3,
        n_observations=n_obs,
    )


class TestEqualWeight:
    def test_basic(self) -> None:
        signals = _signals(["A", "B", "C", "D"])
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            signals, None, PortfolioConstraints(min_cash_buffer=0.0)
        )
        assert result.n_assets == 4
        for w in result.weights.values():
            assert w == pytest.approx(0.25)

    def test_cash_buffer(self) -> None:
        signals = _signals(["A", "B"])
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            signals, None, PortfolioConstraints(min_cash_buffer=0.10)
        )
        assert result.cash_buffer > 0
        assert result.gross_weight <= 0.90 + 1e-6

    def test_max_weight_cap(self) -> None:
        signals = _signals(["A", "B"])
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            signals, None, PortfolioConstraints(max_weight_per_asset=0.30)
        )
        for w in result.weights.values():
            assert w <= 0.30 + 1e-6

    def test_empty_signals_raises(self) -> None:
        alloc = EqualWeightAllocator()
        with pytest.raises(AllocationError):
            alloc.allocate([], None, PortfolioConstraints())

    def test_zero_score_excluded(self) -> None:
        signals = [
            Signal(symbol="A", score=1.0, timestamp=date(2024, 1, 1), strategy_id="s"),
            Signal(symbol="B", score=0.0, timestamp=date(2024, 1, 1), strategy_id="s"),
        ]
        alloc = EqualWeightAllocator()
        result = alloc.allocate(signals, None, PortfolioConstraints())
        assert result.n_assets == 1
        assert "A" in result.weights

    def test_deterministic(self) -> None:
        signals = _signals(["A", "B", "C"])
        alloc = EqualWeightAllocator()
        r1 = alloc.allocate(signals, None, PortfolioConstraints())
        r2 = alloc.allocate(signals, None, PortfolioConstraints())
        assert r1.weights == r2.weights


class TestInverseVolatility:
    def test_basic(self) -> None:
        signals = _signals(["A", "B"])
        cov = _cov_estimate(["A", "B"], vols=[0.01, 0.02])
        alloc = InverseVolatilityAllocator()
        result = alloc.allocate(
            signals, cov,
            PortfolioConstraints(min_cash_buffer=0.0, max_weight_per_asset=0.80, max_weight_per_sleeve=0.90),
        )
        assert result.weight_of("A") > result.weight_of("B")

    def test_no_covariance_raises(self) -> None:
        signals = _signals(["A", "B"])
        alloc = InverseVolatilityAllocator()
        with pytest.raises(AllocationError, match="协方差"):
            alloc.allocate(signals, None, PortfolioConstraints())

    def test_low_vol_gets_more_weight(self) -> None:
        signals = _signals(["A", "B", "C"])
        cov = _cov_estimate(
            ["A", "B", "C"], vols=[0.005, 0.01, 0.02], corr=0.0
        )
        alloc = InverseVolatilityAllocator()
        result = alloc.allocate(
            signals, cov,
            PortfolioConstraints(min_cash_buffer=0.0, max_weight_per_asset=0.80, max_weight_per_sleeve=0.90),
        )
        assert result.weight_of("A") > result.weight_of("B")
        assert result.weight_of("B") > result.weight_of("C")

    def test_all_zero_vol_raises(self) -> None:
        signals = _signals(["A", "B"])
        cov = _cov_estimate(["A", "B"], vols=[0.0, 0.0])
        alloc = InverseVolatilityAllocator()
        with pytest.raises(AllocationError, match="波动率为零"):
            alloc.allocate(signals, cov, PortfolioConstraints())


class TestErc:
    def test_basic(self) -> None:
        signals = _signals(["A", "B", "C"])
        cov = _cov_estimate(
            ["A", "B", "C"], vols=[0.01, 0.02, 0.03], corr=0.3
        )
        alloc = ErcAllocator(max_iter=500)
        result = alloc.allocate(
            signals, cov,
            PortfolioConstraints(min_cash_buffer=0.0, max_weight_per_asset=0.80, max_weight_per_sleeve=0.90),
        )
        assert result.n_assets == 3
        from finboard_backtest.portfolio.allocators import _risk_contributions
        w_vec = np.array([result.weight_of(t) for t in ["A", "B", "C"]])
        rc = _risk_contributions(w_vec, cov.matrix)
        rc_pct = rc / rc.sum() if rc.sum() > 0 else rc
        for val in rc_pct:
            assert abs(val - 1.0 / 3) < 0.08

    def test_no_covariance_raises(self) -> None:
        signals = _signals(["A", "B"])
        alloc = ErcAllocator()
        with pytest.raises(AllocationError, match="协方差"):
            alloc.allocate(signals, None, PortfolioConstraints())

    def test_single_asset(self) -> None:
        signals = _signals(["A"])
        cov = _cov_estimate(["A"], vols=[0.01])
        alloc = ErcAllocator()
        result = alloc.allocate(signals, cov, PortfolioConstraints())
        assert result.weight_of("A") > 0

    def test_equal_vols_equal_weights(self) -> None:
        signals = _signals(["A", "B", "C"])
        cov = _cov_estimate(
            ["A", "B", "C"], vols=[0.02, 0.02, 0.02], corr=0.0
        )
        alloc = ErcAllocator()
        result = alloc.allocate(
            signals, cov,
            PortfolioConstraints(min_cash_buffer=0.0, max_weight_per_asset=0.80, max_weight_per_sleeve=0.90),
        )
        for w in result.weights.values():
            assert abs(w - 1.0 / 3) < 0.02


class TestMakeAllocator:
    def test_equal_weight(self) -> None:
        alloc = make_allocator("equal_weight")
        assert isinstance(alloc, EqualWeightAllocator)

    def test_inverse_volatility(self) -> None:
        alloc = make_allocator("inverse_volatility")
        assert isinstance(alloc, InverseVolatilityAllocator)

    def test_erc(self) -> None:
        alloc = make_allocator("erc")
        assert isinstance(alloc, ErcAllocator)

    def test_unknown_raises(self) -> None:
        with pytest.raises(AllocationError, match="未知分配方法"):
            make_allocator("nonexistent")
