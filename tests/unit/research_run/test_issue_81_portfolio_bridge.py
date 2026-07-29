"""#81 组合结果接入 #80 生命周期的部分成交/拒单闭环。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal

from finboard_backtest.portfolio import (
    AssetLotInfo,
    PortfolioBuildInput,
    PortfolioConstraints,
    Signal,
    SizingInput,
    build_portfolio,
    solve_sizing,
    to_research_constraint_outcomes,
    to_research_rebalance_instructions,
    to_research_targets,
)
from finboard_backtest.research_run import (
    ResearchOrderStatus,
    ResearchRunCoordinator,
)

from .conftest import replace_pipeline_evidence


def test_signal_to_partial_fill_and_actual_position(
    decision_factory, manifest_factory
) -> None:
    manifest = manifest_factory()
    result = build_portfolio(
        PortfolioBuildInput(
            signals=(
                Signal(
                    symbol="510300.SH",
                    score=1.0,
                    confidence=0.8,
                    timestamp=date(2024, 1, 2),
                    strategy_id="issue-81",
                ),
            ),
            method="equal_weight",
            constraints=PortfolioConstraints(
                max_weight_per_asset=0.25,
                max_weight_per_sleeve=0.4,
            ),
            sleeve_map={"510300.SH": "equity"},
        )
    )
    plan = solve_sizing(
        SizingInput(
            target=result.target_after_constraints,
            capital=100_000,
            lot_info={"510300.SH": AssetLotInfo(code="510300.SH", lot_size=100)},
            prices={"510300.SH": 100},
        )
    )
    instructions = to_research_rebalance_instructions(
        plan,
        run_id="RR-issue-81-bridge",
        decision_index=0,
        lot_sizes={"510300.SH": 100},
    )

    base = decision_factory(
        manifest=manifest,
        quantity=Decimal("50"),
        order_quantity=Decimal("100"),
        order_status=ResearchOrderStatus.PARTIALLY_FILLED,
        cash=Decimal("95000"),
        market_value=Decimal("5000"),
    )
    targets_after_constraints = to_research_targets(
        result.target_after_constraints
    )
    decision = replace_pipeline_evidence(
        replace(
            base,
            targets_before_constraints=to_research_targets(
                result.target_before_constraints
            ),
            constraints=to_research_constraint_outcomes(result),
            targets_after_constraints=targets_after_constraints,
            targets_after_risk=targets_after_constraints,
            rebalance_plan=instructions,
            orders=(
                replace(
                    base.orders[0],
                    instruction_id=instructions[0].instruction_id,
                ),
            ),
        ),
        manifest=manifest,
    )

    ResearchRunCoordinator._validate_decision(
        decision,
        manifest=manifest,
        position_quantities=defaultdict(Decimal),
        seen_fill_ids=set(),
    )
    assert decision.orders[0].status is ResearchOrderStatus.PARTIALLY_FILLED
    assert decision.positions[0].quantity == Decimal("50")
    assert decision.ledger.equity == Decimal("100000")
