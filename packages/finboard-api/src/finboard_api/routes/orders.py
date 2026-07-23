"""订单端点 — 列表 / 详情 / 下单 / 撤单。"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_account_id, get_kernel, get_session
from finboard_api.schemas import (
    FillOut,
    OrderCreate,
    OrderOut,
    PageResponse,
)
from finboard_core import TradingKernel
from finboard_persistence import FillRepository, OrderRepository
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderType, Side, TimeInForce

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _order_to_out(order: object) -> OrderOut:
    """领域 Order → OrderOut。"""
    return OrderOut(
        client_order_id=str(order.client_order_id),  # type: ignore[attr-defined]
        account_id=str(order.account_id),  # type: ignore[attr-defined]
        broker_kind=order.broker_kind.value,  # type: ignore[attr-defined]
        symbol=order.symbol.code,  # type: ignore[attr-defined]
        market=order.symbol.market.value,  # type: ignore[attr-defined]
        side=order.side.value,  # type: ignore[attr-defined]
        order_type=order.order_type.value,  # type: ignore[attr-defined]
        quantity=order.quantity,  # type: ignore[attr-defined]
        strategy_id=str(order.strategy_id) if order.strategy_id else None,  # type: ignore[attr-defined]
        price=order.price,  # type: ignore[attr-defined]
        time_in_force=order.time_in_force.value,  # type: ignore[attr-defined]
        position_side=order.position_side.value,  # type: ignore[attr-defined]
        broker_order_id=order.broker_order_id,  # type: ignore[attr-defined]
        filled_quantity=order.filled_quantity,  # type: ignore[attr-defined]
        average_fill_price=order.average_fill_price,  # type: ignore[attr-defined]
        status=order.status.value,  # type: ignore[attr-defined]
        reject_reason=order.reject_reason.value if order.reject_reason else None,  # type: ignore[attr-defined]
        reject_message=order.reject_message,  # type: ignore[attr-defined]
        created_at=order.created_at,  # type: ignore[attr-defined]
        risk_checked_at=order.risk_checked_at,  # type: ignore[attr-defined]
        submitted_at=order.submitted_at,  # type: ignore[attr-defined]
        acknowledged_at=order.acknowledged_at,  # type: ignore[attr-defined]
        updated_at=order.updated_at,  # type: ignore[attr-defined]
        is_active=order.is_active,  # type: ignore[attr-defined]
        is_terminal=order.is_terminal,  # type: ignore[attr-defined]
        remaining_quantity=order.remaining_quantity,  # type: ignore[attr-defined]
    )


@router.get("", response_model=PageResponse[OrderOut])
async def list_orders(
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: AccountId = Depends(get_account_id),
    session: AsyncSession = Depends(get_session),
) -> PageResponse[OrderOut]:
    repo = OrderRepository(session)
    orders, total = await repo.list_all(
        str(account_id),
        status=status,
        symbol=symbol,
        limit=limit,
        offset=offset,
    )
    return PageResponse(
        items=[_order_to_out(o) for o in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{client_order_id}", response_model=OrderOut)
async def get_order(
    client_order_id: str = Path(...),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    repo = OrderRepository(session)
    order = await repo.get(client_order_id)
    if order is None:
        from finboard_shared.exceptions import OrderNotFoundError

        raise OrderNotFoundError(f"订单 {client_order_id} 不存在")
    return _order_to_out(order)


@router.get("/{client_order_id}/fills", response_model=list[FillOut])
async def list_order_fills(
    client_order_id: str = Path(...),
    session: AsyncSession = Depends(get_session),
) -> list[FillOut]:
    repo = FillRepository(session)
    fills = await repo.list_by_order(client_order_id)
    return [
        FillOut(
            fill_id=f.fill_id,
            client_order_id=str(f.client_order_id),
            symbol=f.symbol.code,
            side=f.side.value,
            quantity=f.quantity,
            price=f.price,
            commission=f.commission,
            tax=f.tax,
            broker_order_id=f.broker_order_id,
            filled_at=f.filled_at,
        )
        for f in fills
    ]


@router.post("", response_model=OrderOut, status_code=201)
async def place_order(
    body: OrderCreate,
    kernel: TradingKernel = Depends(get_kernel),
    account_id: AccountId = Depends(get_account_id),
) -> OrderOut:
    """手工下单。

    红线:client_order_id 由 OrderManager 内部生成;下单经完整风控链路。
    """
    request = OrderRequest(
        account_id=account_id,
        symbol=Symbol(code=body.symbol, market=Market(body.market)),
        side=Side(body.side),
        order_type=OrderType(body.order_type),
        quantity=Decimal(str(body.quantity)),
        price=Decimal(str(body.price)) if body.price else None,
        time_in_force=TimeInForce(body.time_in_force),
    )
    order = await kernel.order_manager.place_order(request)
    return _order_to_out(order)


@router.delete("/{client_order_id}", status_code=204)
async def cancel_order(
    client_order_id: str = Path(...),
    kernel: TradingKernel = Depends(get_kernel),
) -> None:
    await kernel.order_manager.cancel_order(client_order_id)
