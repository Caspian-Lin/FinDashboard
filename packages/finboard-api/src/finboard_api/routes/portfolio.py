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
    PortfolioConstraints,
    Signal,
    compute_attribution,
    estimate_covariance,
    make_allocator,
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
    target_volatility: float | None = None
    max_volatility: float | None = None
    rebalance_threshold: float = 0.05
    returns_by_ticker: dict[str, list[float]] | None = None


class WeightOut(BaseModel):
    code: str
    weight: float


class AllocateResponse(BaseModel):
    weights: list[WeightOut]
    cash_buffer: float
    gross_weight: float
    n_assets: int
    contract_version: str
    covariance_shrinkage: float | None = None


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

    constraints = PortfolioConstraints(
        max_weight_per_asset=request.max_weight_per_asset,
        max_weight_per_sleeve=request.max_weight_per_sleeve,
        min_cash_buffer=request.min_cash_buffer,
        max_leverage=request.max_leverage,
        target_volatility=request.target_volatility,
        max_volatility=request.max_volatility,
        rebalance_threshold=request.rebalance_threshold,
    )

    covariance: CovarianceEstimate | None = None
    if request.returns_by_ticker:
        try:
            covariance = estimate_covariance(
                {k: np.array(v, dtype=np.float64) for k, v in request.returns_by_ticker.items()}
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"协方差估计失败: {exc}") from exc

    allocator = make_allocator(request.method)
    try:
        target = allocator.allocate(signals, covariance, constraints)
    except AllocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return AllocateResponse(
        weights=[WeightOut(code=c, weight=w) for c, w in target.weights.items()],
        cash_buffer=target.cash_buffer,
        gross_weight=target.gross_weight,
        n_assets=target.n_assets,
        contract_version=target.contract_version,
        covariance_shrinkage=covariance.shrinkage if covariance else None,
    )


class LotInfoIn(BaseModel):
    code: str
    lot_size: int = 100
    multiplier: float = 1.0


class SizingRequest(BaseModel):
    weights: dict[str, float]
    as_of: date
    strategy_id: str = "api"
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


class SizingResponse(BaseModel):
    trades: list[TradeOut]
    total_capital: float
    cash_before: float
    cash_after: float
    est_commission: float
    est_tax: float
    total_turnover: float
    n_active_trades: int


@router.post("/sizing", response_model=SizingResponse)
async def solve_portfolio_sizing(request: SizingRequest) -> SizingResponse:
    """离散手数求解。"""
    from finboard_backtest.portfolio.contracts import TargetWeight

    target = TargetWeight(
        weights=request.weights,
        as_of=request.as_of,
        strategy_id=request.strategy_id,
    )

    lot_map = {
        li.code: AssetLotInfo(code=li.code, lot_size=li.lot_size, multiplier=li.multiplier)
        for li in request.lot_info
    }

    try:
        plan = solve_sizing(SizingInput(
            target=target,
            capital=request.capital,
            lot_info=lot_map,
            prices=request.prices,
            commission_rate=request.commission_rate,
            commission_min=request.commission_min,
            stamp_tax_rate=request.stamp_tax_rate,
        ))
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
    )


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
