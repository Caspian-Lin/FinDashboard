"""Kill Switch 端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from finboard_api.deps import get_components, get_kernel
from finboard_api.schemas import KillSwitchActivate, KillSwitchOut
from finboard_app.bootstrap import KernelComponents
from finboard_core import TradingKernel
from finboard_shared.types import KillSwitchLevel

router = APIRouter(prefix="/api/kill-switch", tags=["kill-switch"])


@router.get("", response_model=KillSwitchOut)
async def get_kill_switch(
    kernel: TradingKernel = Depends(get_kernel),
    components: KernelComponents = Depends(get_components),
) -> KillSwitchOut:
    level = kernel.kill_switch_level
    return KillSwitchOut(
        level=level.value,
        allows_new_orders=await components.risk_checker.can_place_new_orders(),
        allows_reduce_only=_allows_reduce_only(level),
    )


@router.post("", response_model=KillSwitchOut)
async def activate_kill_switch(
    body: KillSwitchActivate,
    kernel: TradingKernel = Depends(get_kernel),
    components: KernelComponents = Depends(get_components),
) -> KillSwitchOut:
    """激活 Kill Switch — 经交易内核执行,不绕过风控。"""
    level = KillSwitchLevel(body.level)
    await kernel.activate_kill_switch(level, reason=body.reason)
    return KillSwitchOut(
        level=level.value,
        allows_new_orders=await components.risk_checker.can_place_new_orders(),
        allows_reduce_only=_allows_reduce_only(level),
    )


def _allows_reduce_only(level: KillSwitchLevel) -> bool:
    return level in (KillSwitchLevel.OFF, KillSwitchLevel.NO_NEW_ORDERS, KillSwitchLevel.REDUCE_ONLY)
