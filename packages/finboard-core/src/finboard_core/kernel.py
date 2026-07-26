"""``TradingKernel`` —— 把各 manager 串起来,提供 start / stop 入口。

启动顺序(§5.4 完整恢复流程):

1. 连接 broker;
2. 拉取账户/持仓快照;
3. **重启恢复**(若注入 recoverer):逐订单查券商 → 修复 UNKNOWN/遗留状态;
4. 启动 OrderManager 消费回报(加载已修复的活动订单到内存);
5. **启动核对**(若注入 reconciler):本地 ↔ 券商比对,通过才 _ready=True。

红线(AGENTS.md §系统重启后必须先完成核对):

* ``start`` 完成前**禁止**接受新订单 —— 由 ``_ready`` flag 强制;
* 恢复以券商为真值:UNKNOWN + 券商无记录 → REJECTED(不自动重发);
* 恢复异常或核对未通过 → _ready 保持 False。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog

from finboard_broker.base import BrokerAdapter
from finboard_core.account_manager import AccountManager
from finboard_core.bus import EventBus
from finboard_core.order_manager import OrderManager
from finboard_core.position_manager import PositionManager
from finboard_core.protocols import Reconciler, Recoverer, RiskChecker
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
        reconciler: Reconciler | None = None,
        recoverer: Recoverer | None = None,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._credentials = credentials
        self._bus = event_bus or EventBus()
        self._risk = risk_checker
        self._reconciler = reconciler
        self._recoverer = recoverer
        self._audit = audit_repo

        self._ready: bool = False
        self._started_at: datetime | None = None
        # gate 先初始化为 False,OrderManager 通过 lambda 读最新值
        self.order_manager = OrderManager(
            broker=broker,
            order_repo=order_repo,
            fill_repo=fill_repo,
            audit_repo=audit_repo,
            risk_checker=risk_checker,
            event_bus=self._bus,
            account_id=account_id,
            gate=lambda: self._ready,
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

        self._kill_switch: KillSwitchLevel = KillSwitchLevel.OFF

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """启动内核(交易安全红线:核对通过前 _ready 保持 False)。

        流程(§5.4):
        1. 连接 broker;
        2. 拉取账户 / 持仓快照;
        3. **重启恢复**(若注入 recoverer):逐订单查券商 → 修复状态;
        4. 启动 OrderManager 消费回报;
        5. **启动核对**(若注入 reconciler):本地 ↔ 券商比对,通过才 _ready=True。

        恢复异常 → _ready=False,不继续启动。
        """
        if self._ready:
            return
        logger.info("kernel.starting", account_id=str(self._account_id))
        self._started_at = datetime.now(UTC)
        await self._broker.connect(self._account_id, self._credentials)
        # 拉一次券商侧快照,触发 audit / position 落地
        try:
            await self.account_manager.refresh()
            broker_positions = await self._broker.query_positions()
            await self.position_manager.overwrite_from_broker(broker_positions)
        except Exception:
            logger.exception("kernel.initial_snapshot_failed", exc_info=True)

        # 重启恢复:修复 UNKNOWN / 遗留订单状态(在 OrderManager 加载缓存之前)
        if self._recoverer is not None:
            try:
                recovery_report = await self._recoverer.run()
                logger.info(
                    "kernel.recovery_completed", summary=recovery_report.summary()
                )
                if not recovery_report.ok:
                    logger.error(
                        "kernel.recovery_unresolvable",
                        summary=recovery_report.summary(),
                    )
                    self._ready = False
                    return
            except Exception:
                logger.exception("kernel.recovery_exception", exc_info=True)
                self._ready = False
                return

        await self.order_manager.start()

        # 启动核对(红线:核对未通过禁止下单)
        if self._reconciler is not None:
            try:
                report = await self._reconciler.run()
            except Exception:
                logger.exception("kernel.reconcile_exception", exc_info=True)
                await self._audit.add(
                    actor="system",
                    action="reconcile",
                    payload="exception during reconciliation",
                )
                self._ready = False
                return
            if report.ok:
                logger.info(
                    "kernel.reconcile_passed", summary=report.summary()
                )
                await self._audit.add(
                    actor="system",
                    action="reconcile",
                    payload=f"ok=True {report.summary()}",
                )
            else:
                # 核对失败:保持 _ready=False,OrderManager.place_order 会被
                # gate 拒绝(KernelNotReadyError)。需要人工介入或重跑 reconcile。
                logger.error(
                    "kernel.reconcile_failed_blocked",
                    summary=report.summary(),
                )
                await self._audit.add(
                    actor="system",
                    action="reconcile",
                    payload=f"ok=False {report.summary()}",
                )
                self._ready = False
                return
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
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def broker(self) -> BrokerAdapter:
        return self._broker

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
        await self._audit.add(
            actor="system",
            action="kill_switch",
            payload=f"level={level.value} reason={reason}",
        )
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
