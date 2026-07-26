"""账户端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from finboard_api.deps import get_kernel
from finboard_api.schemas import AccountOut
from finboard_core import TradingKernel

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("", response_model=AccountOut)
async def get_account(kernel: TradingKernel = Depends(get_kernel)) -> AccountOut:
    account = await kernel.account_manager.current()
    if account is None:
        account = await kernel.account_manager.refresh()
    return AccountOut(
        account_id=str(account.account_id),
        broker_kind=account.broker_kind.value,
        total_asset=account.total_asset,
        cash=account.cash,
        frozen_cash=account.frozen_cash,
        margin_used=account.margin_used,
        updated_at=account.updated_at,
    )


@router.post("/refresh", response_model=AccountOut)
async def refresh_account(kernel: TradingKernel = Depends(get_kernel)) -> AccountOut:
    account = await kernel.account_manager.refresh()
    return AccountOut(
        account_id=str(account.account_id),
        broker_kind=account.broker_kind.value,
        total_asset=account.total_asset,
        cash=account.cash,
        frozen_cash=account.frozen_cash,
        margin_used=account.margin_used,
        updated_at=account.updated_at,
    )
