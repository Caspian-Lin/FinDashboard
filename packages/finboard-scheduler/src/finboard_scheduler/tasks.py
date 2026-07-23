"""内置定时任务工厂。

每个工厂函数返回一个 :class:`ScheduledTask`,封装对 kernel / order_manager /
reconciler / broker 的调用。

交易安全红线:
* 收盘撤单经 :meth:`OrderManager.cancel_all_active`,走完整风控链路;
* 日终核对发现差异 → 激活 Kill Switch(NO_NEW_ORDERS)暂停次日自动交易;
* 心跳检测断线 → 激活 Kill Switch(NO_NEW_ORDERS)防误下单。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import time
from typing import TYPE_CHECKING

import structlog

from finboard_scheduler.scheduler import ScheduledTask
from finboard_shared.types import KillSwitchLevel

if TYPE_CHECKING:
    from finboard_broker.base import BrokerAdapter
    from finboard_core.kernel import TradingKernel
    from finboard_core.order_manager import OrderManager
    from finboard_core.protocols import Reconciler
    from finboard_persistence.repo import AuditLogRepository
    from finboard_shared.identifiers import AccountId

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------ 盘前检查
def pre_market_check_task(
    *,
    kernel: TradingKernel,
    reconciler: Reconciler | None = None,
    at: time = time(9, 10),
) -> ScheduledTask:
    """盘前检查(9:10):确认 broker 连接 + 账户/持仓快照 + 核对。"""

    async def _run() -> None:
        broker = kernel.broker
        connected = await broker.is_connected()
        if not connected:
            logger.error("pre_market.broker_not_connected")
            await kernel.activate_kill_switch(
                KillSwitchLevel.NO_NEW_ORDERS,
                reason="盘前检查:broker 未连接",
            )
            return

        if reconciler is not None:
            report = await reconciler.run()
            if not report.ok:
                logger.error(
                    "pre_market.reconcile_failed", summary=report.summary()
                )
                await kernel.activate_kill_switch(
                    KillSwitchLevel.NO_NEW_ORDERS,
                    reason=f"盘前核对未通过: {report.summary()}",
                )
            else:
                logger.info("pre_market.reconcile_ok", summary=report.summary())

    return ScheduledTask(
        name="pre_market_check",
        func=_run,
        time=at,
        trading_days_only=True,
    )


# ------------------------------------------------------------------ 收盘撤单
def close_cancel_task(
    *,
    kernel: TradingKernel,
    order_manager: OrderManager,
    at: time = time(14, 55),
) -> ScheduledTask:
    """收盘撤单(14:55):撤销所有活动订单,经完整风控链路。

    交易安全:撤单走 :meth:`OrderManager.cancel_order` 的完整链路,
    包括状态机校验 + broker 撤单请求 + 审计日志。
    """

    async def _run() -> None:
        if not kernel.ready:
            logger.warning("close_cancel.kernel_not_ready")
            return
        cancelled = await order_manager.cancel_all_active()
        logger.info("close_cancel.done", cancelled=len(cancelled), ids=cancelled)

    return ScheduledTask(
        name="close_cancel",
        func=_run,
        time=at,
        trading_days_only=True,
    )


# ------------------------------------------------------------------ 日终核对
def end_of_day_reconcile_task(
    *,
    kernel: TradingKernel,
    reconciler: Reconciler,
    audit_repo: AuditLogRepository,
    account_id: AccountId,
    at: time = time(15, 30),
) -> ScheduledTask:
    """日终核对(15:30):全量本地↔券商核对。

    核对发现差异 → 激活 ``NO_NEW_ORDERS`` Kill Switch,暂停次日自动交易,
    等待人工介入。
    """

    async def _run() -> None:
        report = await reconciler.run()
        await audit_repo.add(
            actor="system",
            action="eod_reconcile",
            payload=report.summary(),
        )
        if not report.ok:
            logger.error("eod_reconcile.mismatch", summary=report.summary())
            await kernel.activate_kill_switch(
                KillSwitchLevel.NO_NEW_ORDERS,
                reason=f"日终核对发现差异: {report.summary()}",
            )
        else:
            logger.info("eod_reconcile.ok", summary=report.summary())

    return ScheduledTask(
        name="end_of_day_reconcile",
        func=_run,
        time=at,
        trading_days_only=True,
    )


# ------------------------------------------------------------------ 连接心跳
def heartbeat_task(
    *,
    kernel: TradingKernel,
    interval: float = 30.0,
) -> ScheduledTask:
    """连接心跳(每 30s):检测 broker 连接状态。

    断线时激活 ``NO_NEW_ORDERS`` Kill Switch 防止误下单。
    不自动重连(重连需协调 order_manager consumer 生命周期,留给人工/重启)。
    """

    async def _run() -> None:
        broker: BrokerAdapter = kernel.broker
        connected = await broker.is_connected()
        if not connected:
            logger.critical("heartbeat.broker_disconnected")
            await kernel.activate_kill_switch(
                KillSwitchLevel.NO_NEW_ORDERS,
                reason="心跳检测:broker 断连",
            )
        else:
            logger.debug("heartbeat.ok")

    return ScheduledTask(
        name="heartbeat",
        func=_run,
        interval=interval,
        trading_days_only=False,
    )


# ------------------------------------------------------------------ 工厂
def create_default_tasks(
    *,
    kernel: TradingKernel,
    order_manager: OrderManager,
    reconciler: Reconciler | None,
    audit_repo: AuditLogRepository,
    account_id: AccountId,
) -> list[ScheduledTask]:
    """创建 4 个内置定时任务(盘前检查 / 收盘撤单 / 日终核对 / 心跳)。"""
    tasks = [
        heartbeat_task(kernel=kernel),
        pre_market_check_task(kernel=kernel, reconciler=reconciler),
        close_cancel_task(kernel=kernel, order_manager=order_manager),
    ]
    if reconciler is not None:
        tasks.append(
            end_of_day_reconcile_task(
                kernel=kernel,
                reconciler=reconciler,
                audit_repo=audit_repo,
                account_id=account_id,
            )
        )
    return tasks


# 类型导出(避免 mypy unused-import)
_ = Callable[[], Awaitable[None]]
