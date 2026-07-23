"""持仓端点。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from finboard_api.deps import get_account_id, get_kernel
from finboard_api.schemas import PageResponse, PositionOut
from finboard_core import TradingKernel
from finboard_shared.identifiers import AccountId

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", response_model=PageResponse[PositionOut])
async def list_positions(
    source: Literal["local", "broker"] = Query(default="local"),
    kernel: TradingKernel = Depends(get_kernel),
    account_id: AccountId = Depends(get_account_id),
) -> PageResponse[PositionOut]:
    if source == "broker":
        positions = await kernel.position_manager.list_broker()
    else:
        positions = await kernel.position_manager.list_local()

    items = [
        PositionOut(
            symbol=p.symbol.code,
            market=p.symbol.market.value,
            position_side=p.position_side.value,
            total_quantity=p.total_quantity,
            available_quantity=p.available_quantity,
            frozen_quantity=p.frozen_quantity,
            average_price=p.average_price,
            market_value=p.market_value,
            unrealized_pnl=p.unrealized_pnl,
            updated_at=p.updated_at,
        )
        for p in positions
    ]
    return PageResponse(items=items, total=len(items), limit=len(items), offset=0)
