"""portfolio/sizing.py 和 risk_budget.py 的单元测试。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    AssetLotInfo,
    PortfolioConstraints,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate
from finboard_backtest.portfolio.risk_budget import (
    needs_rebalance,
    portfolio_volatility,
    risk_concentration_check,
    scale_to_target_volatility,
)
from finboard_backtest.portfolio.sizing import (
    SizingError,
    SizingInput,
    solve_sizing,
)


def _lot_info(codes: list[str]) -> dict[str, AssetLotInfo]:
    return {c: AssetLotInfo(code=c, lot_size=100, multiplier=1) for c in codes}


class TestSolveSizing:
    def test_basic_100k(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50, "B": 0.45},
            as_of=date(2024, 6, 28),
            strategy_id="test",
            cash_buffer=0.05,
        )
        lot = _lot_info(["A", "B"])
        prices = {"A": 10.0, "B": 20.0}
        result = solve_sizing(SizingInput(
            target=target, capital=100_000.0,
            lot_info=lot, prices=prices,
        ))
        assert result.cash_after >= 0
        assert result.total_capital == 100_000.0
        active = [t for t in result.trades if not t.is_noop]
        assert len(active) >= 1

    def test_no_negative_cash(self) -> None:
        target = TargetWeight(
            weights={
                "A": 0.30, "B": 0.30, "C": 0.30,
            },
            as_of=date(2024, 6, 28),
            strategy_id="test",
            cash_buffer=0.10,
        )
        lot = _lot_info(["A", "B", "C"])
        prices = {"A": 50.0, "B": 80.0, "C": 100.0}
        result = solve_sizing(SizingInput(
            target=target, capital=100_000.0,
            lot_info=lot, prices=prices,
        ))
        assert result.cash_after >= -0.01

    def test_500k_tier(self) -> None:
        tier = CAPITAL_TIERS["500k"]
        target = TargetWeight(
            weights={"A": 0.40, "B": 0.30, "C": 0.25},
            as_of=date(2024, 6, 28),
            strategy_id="test",
            cash_buffer=0.05,
        )
        lot = _lot_info(["A", "B", "C"])
        prices = {"A": 15.0, "B": 25.0, "C": 50.0}
        result = solve_sizing(SizingInput(
            target=target, capital=tier.total_capital,
            lot_info=lot, prices=prices,
        ))
        assert result.cash_after >= 0

    def test_discrete_lot_rounding(self) -> None:
        target = TargetWeight(
            weights={"A": 0.999},
            as_of=date(2024, 6, 28),
            strategy_id="test",
        )
        lot = {"A": AssetLotInfo(code="A", lot_size=100, multiplier=1)}
        prices = {"A": 7.77}
        result = solve_sizing(SizingInput(
            target=target, capital=10_000.0,
            lot_info=lot, prices=prices,
        ))
        trade = result.trades[0]
        assert trade.target_shares % 100 == 0

    def test_negative_price_raises(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        lot = _lot_info(["A"])
        with pytest.raises(SizingError, match="价格"):
            solve_sizing(SizingInput(
                target=target, capital=100_000.0,
                lot_info=lot, prices={"A": -1.0},
            ))

    def test_missing_price_raises(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        lot = _lot_info(["A"])
        with pytest.raises(SizingError, match="价格"):
            solve_sizing(SizingInput(
                target=target, capital=100_000.0,
                lot_info=lot, prices={},
            ))

    def test_missing_lot_info_raises(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        with pytest.raises(SizingError, match="手数"):
            solve_sizing(SizingInput(
                target=target, capital=100_000.0,
                lot_info={}, prices={"A": 10.0},
            ))

    def test_capital_tier_lookup(self) -> None:
        from finboard_backtest.portfolio.sizing import get_capital_tier
        tier = get_capital_tier("200k")
        assert tier.total_capital == 200_000.0

    def test_unknown_tier_raises(self) -> None:
        from finboard_backtest.portfolio.sizing import get_capital_tier
        with pytest.raises(SizingError):
            get_capital_tier("999k")

    def test_etf_lot_size_10(self) -> None:
        target = TargetWeight(
            weights={"BOND": 0.90},
            as_of=date(2024, 1, 1), strategy_id="s",
        )
        lot = {"BOND": AssetLotInfo(code="BOND", lot_size=10, multiplier=1)}
        prices = {"BOND": 100.0}
        result = solve_sizing(SizingInput(
            target=target, capital=100_000.0,
            lot_info=lot, prices=prices,
        ))
        trade = result.trades[0]
        assert trade.target_shares % 10 == 0

    def test_futures_multiplier(self) -> None:
        target = TargetWeight(
            weights={"IF2406": 0.30},
            as_of=date(2024, 1, 1), strategy_id="s", cash_buffer=0.70,
        )
        lot = {"IF2406": AssetLotInfo(code="IF2406", lot_size=1, multiplier=200)}
        prices = {"IF2406": 3800.0}
        result = solve_sizing(SizingInput(
            target=target, capital=500_000.0,
            lot_info=lot, prices=prices,
            commission_rate=0.000023,
        ))
        trade = result.trades[0]
        assert trade.target_shares <= 2

    def test_turnover_and_commission(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50, "B": 0.40},
            as_of=date(2024, 1, 1), strategy_id="s",
        )
        lot = _lot_info(["A", "B"])
        prices = {"A": 10.0, "B": 20.0}
        result = solve_sizing(SizingInput(
            target=target, capital=100_000.0,
            lot_info=lot, prices=prices,
        ))
        assert result.total_turnover > 0
        assert result.est_commission >= 0


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


class TestPortfolioVolatility:
    def test_single_asset(self) -> None:
        cov = _cov(["A"], [0.01])
        vol = portfolio_volatility({"A": 1.0}, cov, annualized=False)
        assert vol == pytest.approx(0.01, rel=0.01)

    def test_empty(self) -> None:
        cov = _cov(["A"], [0.01])
        assert portfolio_volatility({}, cov) == 0.0


class TestScaleToTargetVol:
    def test_no_target_returns_original(self) -> None:
        target = TargetWeight(
            weights={"A": 0.50}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        cov = _cov(["A"], [0.01])
        result = scale_to_target_volatility(
            target, cov, PortfolioConstraints()
        )
        assert result.weights == target.weights

    def test_scaling_down(self) -> None:
        target = TargetWeight(
            weights={"A": 0.80},
            as_of=date(2024, 1, 1), strategy_id="s",
            cash_buffer=0.20,
        )
        cov = _cov(["A"], [0.05])
        constraints = PortfolioConstraints(
            target_volatility=0.10,
            min_cash_buffer=0.0,
        )
        result = scale_to_target_volatility(target, cov, constraints)
        assert result.weight_of("A") < 0.80
        assert result.cash_buffer > 0.20


class TestNeedsRebalance:
    def test_small_change_no_rebalance(self) -> None:
        constraints = PortfolioConstraints(rebalance_threshold=0.05)
        current = {"A": 0.50, "B": 0.50}
        target = {"A": 0.52, "B": 0.48}
        assert not needs_rebalance(current, target, constraints)

    def test_large_change_triggers(self) -> None:
        constraints = PortfolioConstraints(rebalance_threshold=0.05)
        current = {"A": 0.50, "B": 0.50}
        target = {"A": 0.60, "B": 0.40}
        assert needs_rebalance(current, target, constraints)


class TestRiskConcentration:
    def test_balanced(self) -> None:
        cov = _cov(["A", "B"], [0.01, 0.01], corr=0.0)
        check = risk_concentration_check({"A": 0.50, "B": 0.50}, cov, threshold=0.60)
        assert check.passed
        assert check.max_risk_contribution <= 0.50 + 0.01

    def test_concentrated(self) -> None:
        cov = _cov(["A", "B"], [0.01, 0.03], corr=0.3)
        check = risk_concentration_check({"A": 0.10, "B": 0.90}, cov)
        assert check.max_risk_contribution > 0.50

    def test_empty(self) -> None:
        cov = _cov(["A"], [0.01])
        check = risk_concentration_check({}, cov)
        assert check.passed
