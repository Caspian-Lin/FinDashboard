"""10万/20万/50万元组合可执行性评估(issue #81)。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    MAX_WEIGHT_EPSILON,
    AssetLotInfo,
    PortfolioConstraints,
    RebalancePlan,
    TargetWeight,
)
from finboard_backtest.portfolio.sizing import (
    PositionSnapshot,
    SizingError,
    SizingInput,
    solve_sizing,
)
from finboard_data.releases import ExecutionMetadata


@dataclass(frozen=True, slots=True)
class FeasiblePosition:
    symbol: str
    quantity: int
    notional_value: float
    actual_weight: float
    target_weight: float
    margin_required: float


@dataclass(frozen=True, slots=True)
class CapitalTierFeasibility:
    tier: str
    capital: float
    feasible: bool
    positions: tuple[FeasiblePosition, ...]
    cash_utilization: float
    tracking_error: float
    unfillable_symbols: tuple[str, ...]
    capacity_pressure: float
    margin_required: float
    estimated_costs: float
    plan: RebalancePlan | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapitalFeasibilityInput:
    target: TargetWeight
    lot_info: dict[str, AssetLotInfo]
    prices: dict[str, float]
    current_positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    constraints: PortfolioConstraints | None = None
    tiers: tuple[str, ...] = ("100k", "200k", "500k")

    def __post_init__(self) -> None:
        unknown = sorted(set(self.tiers) - set(CAPITAL_TIERS))
        if unknown:
            raise SizingError(f"未知资金档位: {unknown}")


def asset_lot_info_from_metadata(
    code: str,
    metadata: ExecutionMetadata,
    *,
    slippage_bps: float = 0.0,
    max_participation: float | None = None,
    available_volume: int | None = None,
    tradable: bool = True,
    unavailable_reason: str | None = None,
) -> AssetLotInfo:
    """把 #58 冻结执行元数据转换成 sizing 输入。"""
    lot_size = int(metadata.lot_size)
    if metadata.lot_size != lot_size:
        raise SizingError(f"{code} lot_size 必须可转换为整数: {metadata.lot_size}")
    return AssetLotInfo(
        code=code,
        lot_size=lot_size,
        multiplier=float(metadata.multiplier),
        margin_rate=(float(metadata.margin_rate) if metadata.margin_rate is not None else None),
        commission_rate=float(metadata.commission_rate),
        commission_min=float(metadata.commission_min),
        stamp_tax_rate=float(metadata.stamp_tax_rate),
        slippage_bps=slippage_bps,
        max_participation=max_participation,
        available_volume=available_volume,
        tradable=tradable,
        unavailable_reason=unavailable_reason,
    )


def _evaluate_tier(
    feasibility_input: CapitalFeasibilityInput,
    tier_name: str,
) -> CapitalTierFeasibility:
    capital = CAPITAL_TIERS[tier_name].total_capital
    try:
        plan = solve_sizing(
            SizingInput(
                target=feasibility_input.target,
                capital=capital,
                lot_info=feasibility_input.lot_info,
                prices=feasibility_input.prices,
                current_positions=feasibility_input.current_positions,
            ),
            constraints=feasibility_input.constraints,
        )
    except (SizingError, ValueError) as exc:
        return CapitalTierFeasibility(
            tier=tier_name,
            capital=capital,
            feasible=False,
            positions=(),
            cash_utilization=0.0,
            tracking_error=1.0,
            unfillable_symbols=tuple(sorted(feasibility_input.target.weights)),
            capacity_pressure=1.0,
            margin_required=0.0,
            estimated_costs=0.0,
            plan=None,
            reasons=(str(exc),),
        )

    positions: list[FeasiblePosition] = []
    actual_weights: dict[str, float] = {}
    unfillable: set[str] = set()
    capacity_ratios: list[float] = []
    reasons: list[str] = []
    for trade in plan.trades:
        actual_weight = trade.target_value / capital
        actual_weights[trade.symbol] = actual_weight
        positions.append(
            FeasiblePosition(
                symbol=trade.symbol,
                quantity=trade.target_shares,
                notional_value=trade.target_value,
                actual_weight=actual_weight,
                target_weight=feasibility_input.target.weight_of(trade.symbol),
                margin_required=trade.margin_required,
            )
        )
        if (
            abs(feasibility_input.target.weight_of(trade.symbol)) > MAX_WEIGHT_EPSILON
            and trade.target_shares == 0
        ):
            unfillable.add(trade.symbol)
            reasons.append(f"{trade.symbol} 最小交易单位超过目标资金")
        if trade.unfilled_shares > 0:
            unfillable.add(trade.symbol)
            if trade.reject_reason:
                reasons.append(f"{trade.symbol}: {trade.reject_reason}")
        requested_delta = abs((trade.requested_target_shares or 0) - trade.current_shares)
        if requested_delta > 0:
            capacity_ratios.append(min(1.0, trade.unfilled_shares / requested_delta))

    all_symbols = set(feasibility_input.target.weights) | set(actual_weights)
    tracking_error = math.sqrt(
        sum(
            (actual_weights.get(symbol, 0.0) - feasibility_input.target.weight_of(symbol)) ** 2
            for symbol in all_symbols
        )
    )
    estimated_costs = plan.est_commission + plan.est_tax + plan.est_slippage
    utilization = 1.0 - plan.cash_after / capital
    feasible = plan.cash_after >= -MAX_WEIGHT_EPSILON and not unfillable
    if not reasons:
        reasons.append("现金/保证金、整数手和成交容量约束均可满足")
    return CapitalTierFeasibility(
        tier=tier_name,
        capital=capital,
        feasible=feasible,
        positions=tuple(positions),
        cash_utilization=utilization,
        tracking_error=tracking_error,
        unfillable_symbols=tuple(sorted(unfillable)),
        capacity_pressure=max(capacity_ratios, default=0.0),
        margin_required=plan.margin_required,
        estimated_costs=estimated_costs,
        plan=plan,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_capital_tiers(
    feasibility_input: CapitalFeasibilityInput,
) -> tuple[CapitalTierFeasibility, ...]:
    """确定性评估 100k/200k/500k,不把不可成交目标当成持仓。"""
    return tuple(
        _evaluate_tier(feasibility_input, tier_name) for tier_name in feasibility_input.tiers
    )


__all__ = [
    "CapitalFeasibilityInput",
    "CapitalTierFeasibility",
    "FeasiblePosition",
    "asset_lot_info_from_metadata",
    "evaluate_capital_tiers",
]
