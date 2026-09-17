"""issue #81:组合约束、退出策略与资金档位回归。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from finboard_backtest.portfolio import (
    AllocationError,
    AssetLotInfo,
    CapitalFeasibilityInput,
    CovarianceEstimate,
    CovarianceFailureMode,
    ExitPositionSnapshot,
    PortfolioBuildInput,
    PortfolioConstraints,
    PositionSnapshot,
    Signal,
    SignalConflictPolicy,
    SizingError,
    SizingInput,
    TargetWeight,
    build_portfolio,
    constraint_impact_summary,
    evaluate_capital_tiers,
    execute_risk_exit_policy,
    solve_sizing,
    to_research_constraint_outcomes,
    to_research_rebalance_instructions,
    to_research_targets,
)
from finboard_backtest.strategy_spec import (
    RiskExitPolicy,
    RiskExitRule,
    RiskExitType,
)

AS_OF = date(2025, 1, 10)


def _signal(symbol: str, score: float = 1.0, confidence: float = 1.0) -> Signal:
    return Signal(
        symbol=symbol,
        score=score,
        confidence=confidence,
        timestamp=AS_OF,
        strategy_id="issue-81",
    )


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.array(
            [
                [0.0004, 0.00005, 0.0],
                [0.00005, 0.0009, 0.0],
                [0.0, 0.0, 0.0001],
            ]
        ),
        tickers=["A", "B", "C"],
        shrinkage=0.2,
        n_observations=252,
    )


def test_target_weight_separates_configured_and_actual_leverage() -> None:
    target = TargetWeight(
        weights={"LONG": 0.8, "SHORT": -0.3},
        as_of=AS_OF,
        strategy_id="s",
        max_leverage=1.5,
        long_only=False,
    )
    assert target.max_leverage == 1.5
    assert target.gross_exposure == pytest.approx(1.1)
    assert target.net_exposure == pytest.approx(0.5)
    with pytest.raises(ValueError, match="gross exposure"):
        TargetWeight(
            weights={"A": 1.01},
            as_of=AS_OF,
            strategy_id="s",
        )
    with pytest.raises(ValueError, match="净权重与现金"):
        TargetWeight(
            weights={"A": 0.8},
            as_of=AS_OF,
            strategy_id="s",
            cash_buffer=0.3,
        )


def test_builder_enforces_sleeve_disabled_conflict_and_cash() -> None:
    result = build_portfolio(
        PortfolioBuildInput(
            signals=(
                _signal("A", 1.0),
                _signal("A", -0.2),
                _signal("B", 1.0),
                _signal("C", 1.0),
            ),
            method="equal_weight",
            constraints=PortfolioConstraints(
                max_weight_per_asset=0.5,
                max_weight_per_sleeve=0.4,
                min_cash_buffer=0.1,
            ),
            sleeve_map={"A": "equity", "B": "equity", "C": "bond"},
            disabled_symbols=frozenset({"C"}),
        )
    )
    target = result.target_after_constraints
    assert set(target.weights) == {"A", "B"}
    assert sum(target.weights.values()) <= 0.4 + 1e-9
    assert target.cash_buffer >= 0.1
    assert any(
        item.constraint == "max_weight_per_sleeve" and item.symbol == "equity" and not item.passed
        for item in result.adjustments
    )
    assert next(item for item in result.resolutions if item.symbol == "C").included is False


def test_conflict_neutralization_and_negative_long_only_signal() -> None:
    result = build_portfolio(
        PortfolioBuildInput(
            signals=(
                _signal("A", 1.0),
                _signal("A", -1.0),
                _signal("B", -1.0),
                _signal("C", 0.5, 0.4),
            ),
            method="equal_weight",
            constraints=PortfolioConstraints(),
            conflict_policy=SignalConflictPolicy.NEUTRALIZE,
        )
    )
    assert set(result.target_after_constraints.weights) == {"C"}
    reasons = {item.symbol: item.reason for item in result.resolutions}
    assert "冲突" in reasons["A"]
    assert "long-only" in reasons["B"]


def test_covariance_fail_closed_and_fail_safe_fallback() -> None:
    signals = (_signal("A"), _signal("B"))
    with pytest.raises(Exception, match="fail_closed"):
        build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method="inverse_volatility",
                constraints=PortfolioConstraints(),
            )
        )
    fallback = build_portfolio(
        PortfolioBuildInput(
            signals=signals,
            method="inverse_volatility",
            constraints=PortfolioConstraints(
                covariance_failure_mode=CovarianceFailureMode.FALLBACK_EQUAL_WEIGHT,
            ),
        )
    )
    assert fallback.covariance_fallback_used
    assert fallback.target_after_constraints.n_assets == 2


def test_singular_or_incomplete_covariance_is_never_silently_used() -> None:
    signals = (_signal("A"), _signal("B"))
    singular = CovarianceEstimate(
        matrix=np.array([[0.01, 0.01], [0.01, 0.01]]),
        tickers=["A", "B"],
        shrinkage=0.0,
        n_observations=100,
    )
    with pytest.raises(AllocationError, match="奇异"):
        build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method="erc",
                constraints=PortfolioConstraints(),
                covariance=singular,
            )
        )
    fallback = build_portfolio(
        PortfolioBuildInput(
            signals=signals,
            method="erc",
            constraints=PortfolioConstraints(
                covariance_failure_mode=CovarianceFailureMode.FALLBACK_EQUAL_WEIGHT,
            ),
            covariance=singular,
        )
    )
    assert fallback.covariance_fallback_used
    assert set(fallback.target_after_constraints.weights) == {"A", "B"}

    incomplete = CovarianceEstimate(
        matrix=np.array([[0.01]]),
        tickers=["A"],
        shrinkage=0.0,
        n_observations=100,
    )
    with pytest.raises(AllocationError, match="缺少信号标的"):
        build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method="inverse_volatility",
                constraints=PortfolioConstraints(),
                covariance=incomplete,
            )
        )


def test_volatility_rebalance_and_risk_outputs_are_wired() -> None:
    result = build_portfolio(
        PortfolioBuildInput(
            signals=(_signal("A"), _signal("B"), _signal("C")),
            method="inverse_volatility",
            constraints=PortfolioConstraints(
                max_weight_per_asset=0.8,
                max_weight_per_sleeve=0.9,
                target_volatility=0.10,
                max_volatility=0.12,
                rebalance_threshold=0.02,
            ),
            covariance=_covariance(),
            sleeve_map={"A": "equity", "B": "equity", "C": "bond"},
            current_weights={"C": 0.49},
            betas={"A": 1.1, "B": 0.9, "C": 0.1},
            max_drawdown=0.08,
        )
    )
    assert result.risk.projected_volatility is not None
    assert result.risk.projected_volatility <= 0.12 + 1e-9
    assert result.risk.beta is not None
    assert result.risk.asset_risk_contribution
    assert set(result.risk.sleeve_risk_contribution) == {"equity", "bond"}
    assert result.risk.max_drawdown == 0.08
    assert any(item.constraint == "volatility" for item in result.adjustments)
    assert any(item.constraint == "rebalance_band" for item in result.adjustments)


def _exit_policy() -> RiskExitPolicy:
    return RiskExitPolicy(
        rules=(
            RiskExitRule(
                rule_type=RiskExitType.PRICE_STOP_LOSS,
                enabled=True,
                threshold=0.10,
                rationale="亏损 10% 退出",
            ),
            RiskExitRule(
                rule_type=RiskExitType.VOLATILITY_STOP,
                enabled=True,
                threshold=0.08,
                lookback=20,
                rationale="ATR/价格过高退出",
            ),
            RiskExitRule(
                rule_type=RiskExitType.TAKE_PROFIT,
                enabled=True,
                threshold=0.20,
                rationale="收益 20% 止盈",
            ),
            RiskExitRule(
                rule_type=RiskExitType.MAX_HOLDING_DAYS,
                enabled=True,
                days=30,
                rationale="持仓超期退出",
            ),
            RiskExitRule(
                rule_type=RiskExitType.PORTFOLIO_DRAWDOWN_DERISK,
                enabled=True,
                threshold=0.15,
                target_gross_exposure=0.20,
                rationale="组合回撤降风险",
            ),
            RiskExitRule(
                rule_type=RiskExitType.COOLDOWN,
                enabled=True,
                days=5,
                rationale="退出后冷却",
            ),
        )
    )


def test_exit_policy_uses_filled_positions_and_only_changes_targets() -> None:
    target = TargetWeight(
        weights={"A": 0.5, "B": 0.4},
        as_of=AS_OF,
        strategy_id="s",
        cash_buffer=0.1,
    )
    positions = (
        ExitPositionSnapshot(
            symbol="A",
            filled_quantity=50,
            average_price=100,
            current_price=85,
            opened_on=date(2024, 12, 1),
            atr=9,
        ),
        ExitPositionSnapshot(
            symbol="B",
            filled_quantity=0,
            average_price=10,
            current_price=5,
            opened_on=date(2024, 12, 1),
        ),
    )
    result = execute_risk_exit_policy(
        policy=_exit_policy(),
        target=target,
        positions=positions,
        as_of=AS_OF,
        portfolio_drawdown=0.20,
    )
    assert "A" not in result.target.weights
    assert result.target.weight_of("B") == pytest.approx(0.20)
    assert positions[0].filled_quantity == 50
    assert result.cooldown_until["A"] == date(2025, 1, 15)
    assert any(
        item.rule_type is RiskExitType.PRICE_STOP_LOSS and item.triggered
        for item in result.decisions
    )
    assert any("实际成交数量为零" in item.reason for item in result.decisions)


def test_take_profit_can_be_disabled_or_triggered() -> None:
    target = TargetWeight(
        weights={"A": 0.5},
        as_of=AS_OF,
        strategy_id="s",
        cash_buffer=0.5,
    )
    position = (
        ExitPositionSnapshot(
            symbol="A",
            filled_quantity=100,
            average_price=100,
            current_price=125,
            opened_on=AS_OF,
        ),
    )
    disabled = RiskExitPolicy(
        rules=(
            RiskExitRule(
                rule_type=RiskExitType.TAKE_PROFIT,
                enabled=False,
                threshold=0.2,
                rationale="禁用止盈",
            ),
        )
    )
    assert execute_risk_exit_policy(
        policy=disabled,
        target=target,
        positions=position,
        as_of=AS_OF,
    ).target.weight_of("A") == 0.5

    enabled = RiskExitPolicy(
        rules=(
            RiskExitRule(
                rule_type=RiskExitType.TAKE_PROFIT,
                enabled=True,
                threshold=0.2,
                rationale="收益 20% 止盈",
            ),
        )
    )
    result = execute_risk_exit_policy(
        policy=enabled,
        target=target,
        positions=position,
        as_of=AS_OF,
    )
    assert result.target.weight_of("A") == 0.0
    assert result.decisions[0].triggered


def test_sizing_uses_current_position_delta_and_margin() -> None:
    unchanged = solve_sizing(
        SizingInput(
            target=TargetWeight(
                weights={"A": 0.5},
                as_of=AS_OF,
                strategy_id="s",
                cash_buffer=0.5,
            ),
            capital=10_000,
            lot_info={"A": AssetLotInfo(code="A", lot_size=100)},
            prices={"A": 10},
            current_positions={"A": PositionSnapshot(code="A", shares=500, market_value=5_000)},
        )
    )
    assert unchanged.trades[0].delta_shares == 0
    assert unchanged.cash_after == pytest.approx(5_000)

    futures = solve_sizing(
        SizingInput(
            target=TargetWeight(
                weights={"FUT": 0.8},
                as_of=AS_OF,
                strategy_id="s",
                cash_buffer=0.2,
            ),
            capital=500_000,
            lot_info={
                "FUT": AssetLotInfo(
                    code="FUT",
                    lot_size=1,
                    multiplier=100,
                    margin_rate=0.10,
                    commission_min=0,
                )
            },
            prices={"FUT": 1_000},
        )
    )
    assert futures.trades[0].target_shares == 4
    assert futures.margin_required == pytest.approx(40_000)
    assert futures.cash_after > 400_000


def test_participation_and_three_capital_tiers_are_deterministic() -> None:
    target = TargetWeight(
        weights={"ETF": 0.5},
        as_of=AS_OF,
        strategy_id="s",
        cash_buffer=0.5,
    )
    lot = {
        "ETF": AssetLotInfo(
            code="ETF",
            lot_size=100,
            commission_min=0,
            slippage_bps=5,
            max_participation=0.1,
            available_volume=1_000,
        )
    }
    results = evaluate_capital_tiers(
        CapitalFeasibilityInput(
            target=target,
            lot_info=lot,
            prices={"ETF": 10},
        )
    )
    repeated = evaluate_capital_tiers(
        CapitalFeasibilityInput(
            target=target,
            lot_info=lot,
            prices={"ETF": 10},
        )
    )
    assert [item.tier for item in results] == ["100k", "200k", "500k"]
    assert results == repeated
    assert all(item.plan is not None for item in results)
    assert all(item.unfillable_symbols == ("ETF",) for item in results)
    assert all(item.capacity_pressure > 0 for item in results)
    assert all(item.estimated_costs > 0 for item in results)


def test_limit_block_margin_shortage_and_short_sizing_fail_closed() -> None:
    target = TargetWeight(
        weights={"BLOCKED": 0.1, "FUT": 0.8},
        as_of=AS_OF,
        strategy_id="s",
        cash_buffer=0.1,
    )
    results = evaluate_capital_tiers(
        CapitalFeasibilityInput(
            target=target,
            lot_info={
                "BLOCKED": AssetLotInfo(
                    code="BLOCKED",
                    lot_size=100,
                    tradable=False,
                    unavailable_reason="跌停不可卖/停牌",
                ),
                "FUT": AssetLotInfo(
                    code="FUT",
                    lot_size=1,
                    multiplier=1_000,
                    margin_rate=0.2,
                ),
            },
            prices={"BLOCKED": 10, "FUT": 1_000},
        )
    )
    assert all(not item.feasible for item in results)
    assert all("BLOCKED" in item.unfillable_symbols for item in results)
    assert all("FUT" in item.unfillable_symbols for item in results)
    assert any("跌停" in reason for reason in results[0].reasons)

    with pytest.raises(SizingError, match="long-only"):
        solve_sizing(
            SizingInput(
                target=TargetWeight(
                    weights={"FUT": -0.5},
                    as_of=AS_OF,
                    strategy_id="s",
                    max_leverage=1.5,
                    long_only=False,
                ),
                capital=100_000,
                lot_info={"FUT": AssetLotInfo(code="FUT", lot_size=1)},
                prices={"FUT": 100},
            )
        )


def test_builder_outputs_map_to_research_run_contracts() -> None:
    result = build_portfolio(
        PortfolioBuildInput(
            signals=(_signal("A"), _signal("B")),
            method="equal_weight",
            constraints=PortfolioConstraints(),
            sleeve_map={"A": "equity", "B": "bond"},
        )
    )
    before = to_research_targets(result.target_before_constraints)
    after = to_research_targets(result.target_after_constraints)
    outcomes = to_research_constraint_outcomes(result)
    assert {item.symbol for item in before} == {"A", "B"}
    assert {item.symbol for item in after} == {"A", "B"}
    assert outcomes
    assert all(item.reason for item in outcomes)
    sizing = solve_sizing(
        SizingInput(
            target=result.target_after_constraints,
            capital=100_000,
            lot_info={
                "A": AssetLotInfo(code="A"),
                "B": AssetLotInfo(code="B"),
            },
            prices={"A": 10, "B": 20},
        )
    )
    instructions = to_research_rebalance_instructions(
        sizing,
        run_id="RR-issue-81",
        decision_index=0,
        lot_sizes={"A": 100, "B": 100},
    )
    assert instructions
    assert all(item.instruction_id.startswith("RR-") for item in instructions)
    assert all(item.lot_size == 100 for item in instructions)
    assert constraint_impact_summary(result)
