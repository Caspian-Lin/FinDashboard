"""组合构建层 API 端点(issue #59)。

提供目标权重分配 / 离散手数求解 / 绩效归因计算。
本端点纯计算,不连接数据库,不触及交易红线。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from finboard_backtest.portfolio import (
    AllocationError,
    AssetLotInfo,
    CapitalFeasibilityInput,
    CovarianceFailureMode,
    PortfolioBuildInput,
    PortfolioConstraints,
    Signal,
    SignalConflictPolicy,
    build_portfolio,
    compute_attribution,
    estimate_covariance,
    evaluate_capital_tiers,
    solve_sizing,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate
from finboard_backtest.portfolio.sizing import SizingError, SizingInput

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class SignalIn(BaseModel):
    symbol: str
    score: float
    confidence: float = 1.0


class AllocateRequest(BaseModel):
    signals: list[SignalIn]
    method: Literal["equal_weight", "inverse_volatility", "erc"] = "equal_weight"
    as_of: date
    strategy_id: str = "api"
    max_weight_per_asset: float = 0.25
    max_weight_per_sleeve: float = 0.40
    min_cash_buffer: float = 0.05
    max_leverage: float = 1.0
    long_only: bool = True
    target_volatility: float | None = None
    max_volatility: float | None = None
    rebalance_threshold: float = 0.05
    min_weight_to_trade: float = 0.001
    returns_by_ticker: dict[str, list[float]] | None = None
    sleeve_map: dict[str, str] = Field(default_factory=dict)
    disabled_symbols: list[str] = Field(default_factory=list)
    current_weights: dict[str, float] = Field(default_factory=dict)
    target_gross_exposure: float | None = None
    betas: dict[str, float] = Field(default_factory=dict)
    max_drawdown: float = Field(default=0.0, ge=0, le=1)
    max_risk_contribution: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="小于 1 时启用硬上限并要求提供可用协方差输入",
    )
    conflict_policy: Literal["net", "neutralize"] = "net"
    covariance_failure_mode: Literal["fail_closed", "fallback_equal_weight"] = "fail_closed"


class WeightOut(BaseModel):
    code: str
    weight: float


class ConstraintAdjustmentOut(BaseModel):
    constraint: str
    symbol: str | None
    before_value: float
    after_value: float
    limit: float | None
    passed: bool
    reason: str


class RiskReportOut(BaseModel):
    projected_volatility: float | None
    beta: float | None
    asset_risk_contribution: dict[str, float]
    sleeve_risk_contribution: dict[str, float]
    max_asset_risk_contribution: float | None
    concentration: float
    max_drawdown: float
    constraint_impact: float


class AllocateResponse(BaseModel):
    weights: list[WeightOut]
    weights_before_constraints: list[WeightOut]
    cash_buffer: float
    gross_weight: float
    net_weight: float
    configured_max_leverage: float
    n_assets: int
    contract_version: str
    covariance_shrinkage: float | None = None
    covariance_fallback_used: bool
    adjustments: list[ConstraintAdjustmentOut]
    risk: RiskReportOut


@router.post("/allocate", response_model=AllocateResponse)
async def allocate_portfolio(request: AllocateRequest) -> AllocateResponse:
    """计算目标权重分配。"""
    if not request.signals:
        raise HTTPException(status_code=400, detail="signals 不能为空")

    signals = [
        Signal(
            symbol=s.symbol,
            score=s.score,
            confidence=s.confidence,
            timestamp=request.as_of,
            strategy_id=request.strategy_id,
        )
        for s in request.signals
    ]

    try:
        constraints = PortfolioConstraints(
            max_weight_per_asset=request.max_weight_per_asset,
            max_weight_per_sleeve=request.max_weight_per_sleeve,
            min_cash_buffer=request.min_cash_buffer,
            max_leverage=request.max_leverage,
            target_volatility=request.target_volatility,
            max_volatility=request.max_volatility,
            rebalance_threshold=request.rebalance_threshold,
            min_weight_to_trade=request.min_weight_to_trade,
            max_risk_contribution=request.max_risk_contribution,
            long_only=request.long_only,
            covariance_failure_mode=CovarianceFailureMode(
                request.covariance_failure_mode
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    covariance: CovarianceEstimate | None = None
    if request.returns_by_ticker:
        try:
            covariance = estimate_covariance(
                {k: np.array(v, dtype=np.float64) for k, v in request.returns_by_ticker.items()}
            )
        except Exception as exc:
            if request.covariance_failure_mode == CovarianceFailureMode.FAIL_CLOSED.value:
                raise HTTPException(status_code=400, detail=f"协方差估计失败: {exc}") from exc
            covariance = None

    try:
        result = build_portfolio(
            PortfolioBuildInput(
                signals=tuple(signals),
                method=request.method,
                constraints=constraints,
                covariance=covariance,
                sleeve_map=request.sleeve_map,
                disabled_symbols=frozenset(request.disabled_symbols),
                current_weights=request.current_weights,
                target_gross_exposure=request.target_gross_exposure,
                betas=request.betas,
                max_drawdown=request.max_drawdown,
                conflict_policy=SignalConflictPolicy(request.conflict_policy),
            )
        )
    except (AllocationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target = result.target_after_constraints

    return AllocateResponse(
        weights=[WeightOut(code=c, weight=w) for c, w in target.weights.items()],
        weights_before_constraints=[
            WeightOut(code=c, weight=w) for c, w in result.target_before_constraints.weights.items()
        ],
        cash_buffer=target.cash_buffer,
        gross_weight=target.gross_weight,
        net_weight=target.net_exposure,
        configured_max_leverage=target.max_leverage,
        n_assets=target.n_assets,
        contract_version=target.contract_version,
        covariance_shrinkage=(
            covariance.shrinkage
            if covariance is not None and not result.covariance_fallback_used
            else None
        ),
        covariance_fallback_used=result.covariance_fallback_used,
        adjustments=[
            ConstraintAdjustmentOut(
                constraint=item.constraint,
                symbol=item.symbol,
                before_value=item.before_value,
                after_value=item.after_value,
                limit=item.limit,
                passed=item.passed,
                reason=item.reason,
            )
            for item in result.adjustments
        ],
        risk=RiskReportOut(
            projected_volatility=result.risk.projected_volatility,
            beta=result.risk.beta,
            asset_risk_contribution=result.risk.asset_risk_contribution,
            sleeve_risk_contribution=result.risk.sleeve_risk_contribution,
            max_asset_risk_contribution=result.risk.max_asset_risk_contribution,
            concentration=result.risk.concentration,
            max_drawdown=result.risk.max_drawdown,
            constraint_impact=result.risk.constraint_impact,
        ),
    )


class LotInfoIn(BaseModel):
    code: str
    lot_size: int = 100
    multiplier: float = 1.0
    margin_rate: float | None = Field(default=None, gt=0, le=1)
    commission_rate: float | None = Field(default=None, ge=0)
    commission_min: float | None = Field(default=None, ge=0)
    stamp_tax_rate: float | None = Field(default=None, ge=0)
    slippage_bps: float = Field(default=0, ge=0)
    max_participation: float | None = Field(default=None, gt=0, le=1)
    available_volume: int | None = Field(default=None, ge=0)
    tradable: bool = True
    unavailable_reason: str | None = None


class SizingRequest(BaseModel):
    weights: dict[str, float]
    as_of: date
    strategy_id: str = "api"
    max_leverage: float = Field(default=1.0, ge=1)
    long_only: bool = True
    capital: float = Field(..., gt=0)
    lot_info: list[LotInfoIn]
    prices: dict[str, float]
    commission_rate: float = 0.0003
    commission_min: float = 5.0
    stamp_tax_rate: float = 0.0005


class TradeOut(BaseModel):
    symbol: str
    delta_shares: int
    target_shares: int
    current_shares: int
    target_value: float
    current_value: float
    delta_value: float
    requested_target_shares: int | None
    unfilled_shares: int
    reject_reason: str | None
    estimated_slippage: float
    margin_required: float


class SizingResponse(BaseModel):
    trades: list[TradeOut]
    total_capital: float
    cash_before: float
    cash_after: float
    est_commission: float
    est_tax: float
    total_turnover: float
    n_active_trades: int
    est_slippage: float
    margin_required: float


@router.post("/sizing", response_model=SizingResponse)
async def solve_portfolio_sizing(request: SizingRequest) -> SizingResponse:
    """离散手数求解。"""
    from finboard_backtest.portfolio.contracts import TargetWeight

    try:
        target = TargetWeight(
            weights=request.weights,
            as_of=request.as_of,
            strategy_id=request.strategy_id,
            max_leverage=request.max_leverage,
            long_only=request.long_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lot_map = {
        li.code: AssetLotInfo(
            code=li.code,
            lot_size=li.lot_size,
            multiplier=li.multiplier,
            margin_rate=li.margin_rate,
            commission_rate=li.commission_rate,
            commission_min=li.commission_min,
            stamp_tax_rate=li.stamp_tax_rate,
            slippage_bps=li.slippage_bps,
            max_participation=li.max_participation,
            available_volume=li.available_volume,
            tradable=li.tradable,
            unavailable_reason=li.unavailable_reason,
        )
        for li in request.lot_info
    }

    try:
        plan = solve_sizing(
            SizingInput(
                target=target,
                capital=request.capital,
                lot_info=lot_map,
                prices=request.prices,
                commission_rate=request.commission_rate,
                commission_min=request.commission_min,
                stamp_tax_rate=request.stamp_tax_rate,
            )
        )
    except SizingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SizingResponse(
        trades=[
            TradeOut(
                symbol=t.symbol,
                delta_shares=t.delta_shares,
                target_shares=t.target_shares,
                current_shares=t.current_shares,
                target_value=t.target_value,
                current_value=t.current_value,
                delta_value=t.delta_value,
                requested_target_shares=t.requested_target_shares,
                unfilled_shares=t.unfilled_shares,
                reject_reason=t.reject_reason,
                estimated_slippage=t.estimated_slippage,
                margin_required=t.margin_required,
            )
            for t in plan.trades
        ],
        total_capital=plan.total_capital,
        cash_before=plan.cash_before,
        cash_after=plan.cash_after,
        est_commission=plan.est_commission,
        est_tax=plan.est_tax,
        total_turnover=plan.total_turnover,
        n_active_trades=plan.n_active_trades,
        est_slippage=plan.est_slippage,
        margin_required=plan.margin_required,
    )


class FeasibilityRequest(BaseModel):
    weights: dict[str, float]
    as_of: date
    strategy_id: str = "api"
    max_leverage: float = Field(default=1.0, ge=1)
    long_only: bool = True
    lot_info: list[LotInfoIn]
    prices: dict[str, float]


class TierFeasibilityOut(BaseModel):
    tier: str
    capital: float
    feasible: bool
    cash_utilization: float
    tracking_error: float
    unfillable_symbols: list[str]
    capacity_pressure: float
    margin_required: float
    estimated_costs: float
    reasons: list[str]


@router.post("/feasibility", response_model=list[TierFeasibilityOut])
async def evaluate_portfolio_feasibility(
    request: FeasibilityRequest,
) -> list[TierFeasibilityOut]:
    """固定评估 10万/20万/50万元的整数手、费用、容量与保证金。"""
    from finboard_backtest.portfolio.contracts import TargetWeight

    try:
        target = TargetWeight(
            weights=request.weights,
            as_of=request.as_of,
            strategy_id=request.strategy_id,
            max_leverage=request.max_leverage,
            long_only=request.long_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lot_map = {
        item.code: AssetLotInfo(
            code=item.code,
            lot_size=item.lot_size,
            multiplier=item.multiplier,
            margin_rate=item.margin_rate,
            commission_rate=item.commission_rate,
            commission_min=item.commission_min,
            stamp_tax_rate=item.stamp_tax_rate,
            slippage_bps=item.slippage_bps,
            max_participation=item.max_participation,
            available_volume=item.available_volume,
            tradable=item.tradable,
            unavailable_reason=item.unavailable_reason,
        )
        for item in request.lot_info
    }
    try:
        results = evaluate_capital_tiers(
            CapitalFeasibilityInput(
                target=target,
                lot_info=lot_map,
                prices=request.prices,
            )
        )
    except SizingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        TierFeasibilityOut(
            tier=item.tier,
            capital=item.capital,
            feasible=item.feasible,
            cash_utilization=item.cash_utilization,
            tracking_error=item.tracking_error,
            unfillable_symbols=list(item.unfillable_symbols),
            capacity_pressure=item.capacity_pressure,
            margin_required=item.margin_required,
            estimated_costs=item.estimated_costs,
            reasons=list(item.reasons),
        )
        for item in results
    ]


class AttributionRequest(BaseModel):
    weights_history: list[dict[str, float]]
    returns_by_ticker: dict[str, list[float]]
    sleeve_map: dict[str, str] | None = None


class AssetContributionOut(BaseModel):
    code: str
    weight: float
    return_contribution: float
    risk_contribution: float
    turnover_contribution: float
    drawdown_contribution: float


class SleeveContributionOut(BaseModel):
    sleeve_name: str
    weight: float
    return_contribution: float
    risk_contribution: float
    turnover_contribution: float
    drawdown_contribution: float
    n_assets: int


class AttributionResponse(BaseModel):
    by_asset: list[AssetContributionOut]
    by_sleeve: list[SleeveContributionOut]
    total_return: float
    total_risk: float
    total_turnover: float
    max_drawdown: float
    cash_utilization: float
    leverage_ratio: float


@router.post("/attribution", response_model=AttributionResponse)
async def compute_portfolio_attribution(
    request: AttributionRequest,
) -> AttributionResponse:
    """计算绩效归因分解。"""
    if not request.weights_history:
        raise HTTPException(status_code=400, detail="weights_history 不能为空")

    returns_arrays = {
        k: np.array(v, dtype=np.float64) for k, v in request.returns_by_ticker.items()
    }

    try:
        covariance = estimate_covariance(returns_arrays)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"协方差估计失败: {exc}") from exc

    report = compute_attribution(
        weights_history=request.weights_history,
        returns_by_ticker=returns_arrays,
        covariance=covariance,
        sleeve_map=request.sleeve_map,
    )

    return AttributionResponse(
        by_asset=[
            AssetContributionOut(
                code=ac.code,
                weight=ac.weight,
                return_contribution=ac.return_contribution,
                risk_contribution=ac.risk_contribution,
                turnover_contribution=ac.turnover_contribution,
                drawdown_contribution=ac.drawdown_contribution,
            )
            for ac in report.by_asset
        ],
        by_sleeve=[
            SleeveContributionOut(
                sleeve_name=sc.sleeve_name,
                weight=sc.weight,
                return_contribution=sc.return_contribution,
                risk_contribution=sc.risk_contribution,
                turnover_contribution=sc.turnover_contribution,
                drawdown_contribution=sc.drawdown_contribution,
                n_assets=sc.n_assets,
            )
            for sc in report.by_sleeve
        ],
        total_return=report.total_return,
        total_risk=report.total_risk,
        total_turnover=report.total_turnover,
        max_drawdown=report.max_drawdown,
        cash_utilization=report.cash_utilization,
        leverage_ratio=report.leverage_ratio,
    )
