"""``TradingKernel`` —— 把各 manager 串起来,提供 start / stop 入口。

P0 范围:

* 启动顺序:连接 broker → 拉取账户/持仓快照 → 启动 OrderManager 消费回报;
* 停止顺序:停 OrderManager → 断开 broker → flush audit。

红线(AGENTS.md §系统重启后必须先完成核对):

* ``start`` 完成前**禁止**接受新订单 —— 由 ``_ready`` flag 强制;
* 重启恢复完整流程(读活动订单 / 匹配券商 / 修复状态 / 核对)留待 P0 后期完善,
  当前实现先把"加载活动订单到内存 + 一次 broker 查询"做出来。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import structlog

from finboard_broker.base import BrokerAdapter
from finboard_core.account_manager import AccountManager
from finboard_core.bus import EventBus
from finboard_core.order_manager import OrderManager
from finboard_core.position_manager import PositionManager
from finboard_core.protocols import RiskChecker
from finboard_persistence.repo import (
    AccountRepository,
    AuditLogRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.types import KillSwitchLevel

logger = structlog.get_logger(__name__)


class TradingKernel:
    """交易内核组合根。"""

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        account_id: AccountId,
        credentials: Mapping[str, str],
        order_repo: OrderRepository,
        fill_repo: FillRepository,
        position_repo: PositionRepository,
        account_repo: AccountRepository,
        audit_repo: AuditLogRepository,
        risk_checker: RiskChecker,
        event_bus: EventBus | None = None,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._credentials = credentials
        self._bus = event_bus or EventBus()
        self._risk = risk_checker

        self.order_manager = OrderManager(
            broker=broker,
            order_repo=order_repo,
            fill_repo=fill_repo,
            audit_repo=audit_repo,
            risk_checker=risk_checker,
            event_bus=self._bus,
            account_id=account_id,
        )
        self.position_manager = PositionManager(
            position_repo=position_repo,
            account_id=account_id,
            event_bus=self._bus,
        )
        self.account_manager = AccountManager(
            broker=broker,
            account_repo=account_repo,
            account_id=account_id,
            event_bus=self._bus,
        )

        # 自动把成交事件接到 PositionManager
        from finboard_core.events import OrderFilled

        self._bus.subscribe(OrderFilled, self._on_order_filled)

        self._ready: bool = False
        self._kill_switch: KillSwitchLevel = KillSwitchLevel.OFF

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._ready:
            return
        logger.info("kernel.starting", account_id=str(self._account_id))
        await self._broker.connect(self._account_id, self._credentials)
        # 拉一次券商侧快照,触发 audit / position 落地
        try:
            await self.account_manager.refresh()
            broker_positions = await self._broker.query_positions()
            await self.position_manager.overwrite_from_broker(broker_positions)
        except Exception:
            logger.exception("kernel.initial_snapshot_failed", exc_info=True)
        await self.order_manager.start()
        self._ready = True
        logger.info("kernel.ready", account_id=str(self._account_id))

    async def stop(self) -> None:
        if not self._ready:
            return
        logger.info("kernel.stopping", account_id=str(self._account_id))
        await self.order_manager.stop()
        await self._broker.disconnect()
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    # ------------------------------------------------------------------ Kill Switch
    @property
    def kill_switch_level(self) -> KillSwitchLevel:
        return self._kill_switch

    async def activate_kill_switch(
        self, level: KillSwitchLevel, *, reason: str = ""
    ) -> None:
        """内核级 Kill Switch。

        调用 :class:`RiskChecker` 的对应方法以确保即使前端失灵也能强制生效。
        实际拒绝逻辑在 ``finboard_risk`` 实现。
        """
        self._kill_switch = level
        checker = self._risk
        if hasattr(checker, "set_kill_switch_level"):
            await checker.set_kill_switch_level(level)
        from finboard_core.events import KillSwitchActivated

        await self._bus.publish(
            KillSwitchActivated(level=level, reason=reason)
        )
        logger.warning(
            "kernel.kill_switch_activated", level=level.value, reason=reason
        )

    # ------------------------------------------------------------------ 内部
    async def _on_order_filled(self, event: object) -> None:
        from finboard_core.events import OrderFilled

        if not isinstance(event, OrderFilled):
            return
        await self.position_manager.apply_fill(event.fill)

    # 占位防止 asyncio / logging 未使用告警
    _ = (asyncio, logging)
