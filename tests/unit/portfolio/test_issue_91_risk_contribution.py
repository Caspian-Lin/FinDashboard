"""#91 单资产风险贡献从审计指标升级为 fail-closed 硬约束。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from finboard_backtest.portfolio import (
    AllocationError,
    CovarianceEstimate,
    PortfolioBuildInput,
    PortfolioConstraints,
    Signal,
    build_portfolio,
    enforce_risk_contribution_cap,
)
from finboard_backtest.portfolio.risk_budget import RiskBudgetError


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.09, 0.01, 0.01]),
        tickers=["A", "B", "C"],
        shrinkage=0.0,
        n_observations=252,
    )


def _signals() -> tuple[Signal, ...]:
    return tuple(
        Signal(
            symbol=symbol,
            score=1.0,
            timestamp=date(2025, 1, 2),
            strategy_id="issue-91",
        )
        for symbol in ("A", "B", "C")
    )


def test_projection_only_reduces_exposure_and_enforces_cap() -> None:
    original = {"A": 0.30, "B": 0.30, "C": 0.30}

    result = enforce_risk_contribution_cap(
        original,
        _covariance(),
        threshold=0.40,
    )

    assert result.converged
    assert result.before_max_contribution > 0.80
    assert result.after_max_contribution <= 0.40 + 1e-9
    assert result.weights["A"] < original["A"]
    assert all(result.weights[symbol] <= weight for symbol, weight in original.items())


def test_portfolio_builder_persists_adjusted_hard_constraint() -> None:
    result = build_portfolio(
        PortfolioBuildInput(
            signals=_signals(),
            method="equal_weight",
            covariance=_covariance(),
            sleeve_map={"A": "a", "B": "b", "C": "c"},
            constraints=PortfolioConstraints(
                max_weight_per_asset=1.0,
                max_weight_per_sleeve=1.0,
                max_risk_contribution=0.40,
            ),
        )
    )

    audit = next(
        item
        for item in result.adjustments
        if item.constraint == "max_risk_contribution"
    )
    assert audit.before_value > 0.80
    assert audit.after_value <= 0.40 + 1e-9
    assert audit.passed
    assert result.risk.max_asset_risk_contribution is not None
    assert result.risk.max_asset_risk_contribution <= 0.40 + 1e-9
    assert result.target_after_constraints.weight_of("A") < (
        result.target_before_constraints.weight_of("A")
    )


def test_missing_covariance_fails_closed_when_cap_is_active() -> None:
    with pytest.raises(AllocationError, match="风险贡献硬约束缺少"):
        build_portfolio(
            PortfolioBuildInput(
                signals=_signals(),
                method="equal_weight",
                constraints=PortfolioConstraints(
                    max_weight_per_asset=1.0,
                    max_weight_per_sleeve=1.0,
                    max_risk_contribution=0.40,
                ),
            )
        )


def test_mathematically_infeasible_cap_fails_closed() -> None:
    with pytest.raises(RiskBudgetError, match="风险贡献上限不可行"):
        enforce_risk_contribution_cap(
            {"A": 0.30, "B": 0.30, "C": 0.30},
            _covariance(),
            threshold=0.30,
        )


def test_non_positive_portfolio_variance_fails_closed() -> None:
    covariance = CovarianceEstimate(
        matrix=np.zeros((3, 3)),
        tickers=["A", "B", "C"],
        shrinkage=0.0,
        n_observations=252,
    )
    with pytest.raises(RiskBudgetError, match="组合方差必须为正且有限"):
        enforce_risk_contribution_cap(
            {"A": 0.30, "B": 0.30, "C": 0.30},
            covariance,
            threshold=0.40,
        )
