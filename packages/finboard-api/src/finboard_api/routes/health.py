"""健康检查端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from finboard_api.deps import get_kernel
from finboard_api.schemas import HealthOut
from finboard_core import TradingKernel

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
async def health(kernel: TradingKernel = Depends(get_kernel)) -> HealthOut:
    ready = kernel.ready
    started_at = kernel.started_at
    active_orders = len(kernel.order_manager._inflight)
    return HealthOut(
        status="ok" if ready else "not_ready",
        kernel_ready=ready,
        kill_switch_level=kernel.kill_switch_level.value,
        broker_connected=await kernel.broker.is_connected(),
        broker_kind=kernel.broker.kind.value,
        active_orders=active_orders,
        started_at=started_at,
    )
