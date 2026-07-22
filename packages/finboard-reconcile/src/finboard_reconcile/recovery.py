"""重启恢复引擎 —— 逐订单修复本地状态,使本地与券商一致(phase1_doc.md §5.4 步骤 7-8)。

与 :class:`ReconciliationEngine`(只读核对)的区别:

* ReconciliationEngine 是**只读比较**:发现差异 → 报告 → 阻塞交易等待人工;
* RecoveryEngine 是**主动修复**:查询券商 → 以券商为真值 → 修复本地订单状态 → 持久化。

恢复逻辑以券商为唯一真值(AGENTS.md §持仓的最终真实来源是券商查询):

+-------------------+-------------------+----------------------------------------+
| 本地状态           | 券商查询结果       | 修复动作                               |
+===================+===================+========================================+
| UNKNOWN           | 券商有(state=X)  | → X,同步 filled_quantity / avg_price  |
| UNKNOWN           | 券商无             | → REJECTED(券商未收到,安全终止)       |
| SUBMITTING        | 券商无             | → REJECTED(未成功提交)                |
| SUBMITTED         | 券商无             | → REJECTED(券商未接收)                |
| SUBMITTING/       | 券商有(state=X)  | → X,同步 filled_quantity              |
| SUBMITTED/        |                   |                                        |
| ACKNOWLEDGED+     |                   |                                        |
| 任意活动状态       | 查询异常           | 中止恢复 → _ready 保持 False           |
+-------------------+-------------------+----------------------------------------+

红线:

* ``query_order(cid) → None`` 意味着券商确认不存在此订单 → 安全标 REJECTED;
  不会自动重发(AGENTS.md §下单请求超时后禁止无条件重试)。
* 查询本身失败(网络异常等)→ 抛异常 → 整个恢复中止 → kernel._ready 保持 False。
* 不重建 Fill 记录(成交明细表);仅同步 order.filled_quantity。持仓由券商独立同步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from finboard_broker.base import BrokerAdapter
from finboard_core.state_machine import OrderStateMachine
from finboard_persistence.repo import (
    AuditLogRepository,
    OrderRepository,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Order
from finboard_shared.types import OrderStatus, RejectReason

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrderRepair:
    """单笔订单的修复记录。"""

    client_order_id: str
    from_status: OrderStatus
    to_status: OrderStatus
    reason: str
    synced_filled_quantity: Decimal | None = None


@dataclass
class RecoveryReport:
    """恢复汇总。"""

    repairs: list[OrderRepair] = field(default_factory=list)
    #: 无法修复的订单 cid(例如状态机不允许的迁移)—— 需人工介入
    unresolvable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolvable

    def summary(self) -> str:
        return (
            f"recovery ok={self.ok} "
            f"repaired={len(self.repairs)} "
            f"unresolvable={len(self.unresolvable)}"
        )


class RecoveryEngine:
    """重启恢复引擎(§5.4 步骤 7-8)。

    在 :meth:`TradingKernel.start` 中于 ``order_manager.start()`` **之前**调用,
    确保 OrderManager 加载到内存的是已修复的正确状态。
    """

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        account_id: AccountId,
        order_repo: OrderRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._orders = order_repo
        self._audit = audit_repo

    async def run(self) -> RecoveryReport:
        """加载本地活动订单 → 查券商 → 修复状态 → 持久化 → 返回报告。"""
        report = RecoveryReport()
        account_id_str = str(self._account_id)

        local_orders = await self._orders.list_active(account_id_str)
        if not local_orders:
            logger.info("recovery.no_active_orders")
            return report

        logger.info(
            "recovery.starting", active_orders=len(local_orders)
        )

        for order in local_orders:
            try:
                await self._recover_one(order, report)
            except Exception:
                logger.exception(
                    "recovery.order_failed",
                    client_order_id=str(order.client_order_id),
                )
                report.unresolvable.append(str(order.client_order_id))

        logger.info("recovery.completed", summary=report.summary())
        return report

    async def _recover_one(
        self, order: Order, report: RecoveryReport
    ) -> None:
        cid = str(order.client_order_id)

        # 查券商(查询类方法允许内部重试;彻底失败会抛异常 → 上层 catch)
        broker_order = await self._broker.query_order(cid)

        if broker_order is None:
            await self._repair_not_on_broker(order, report)
        else:
            await self._repair_from_broker(order, broker_order, report)

    # ------------------------------------------------------------------ 修复分支
    async def _repair_not_on_broker(
        self, order: Order, report: RecoveryReport
    ) -> None:
        """券商确认不存在此订单 → 安全标 REJECTED。

        触发场景:
        - UNKNOWN(下单超时,券商实际没收到);
        - SUBMITTING(崩溃发生在 broker.place_order 之前);
        - SUBMITTED(broker 返回了但内部丢失 —— 极罕见)。
        """
        cid = str(order.client_order_id)
        target = OrderStatus.REJECTED

        if not OrderStateMachine.can_transition(order.status, target):
            # 已是终态或状态机不允许 —— 记录但不修复
            report.unresolvable.append(cid)
            logger.warning(
                "recovery.cannot_reject",
                client_order_id=cid,
                status=order.status.value,
            )
            return

        old_status = order.status
        OrderStateMachine.check_transition(order.status, target)
        order.status = target
        order.reject_reason = RejectReason.BROKER_REJECTED
        order.reject_message = "重启恢复:券商无此订单记录"
        order.touch()
        await self._orders.update_state(order)
        await self._audit_repair(order, old_status, target, "broker 无此订单")
        report.repairs.append(
            OrderRepair(
                client_order_id=cid,
                from_status=old_status,
                to_status=target,
                reason="券商无此订单 → REJECTED",
            )
        )
        logger.info(
            "recovery.rejected_not_on_broker",
            client_order_id=cid,
            from_status=old_status.value,
        )

    async def _repair_from_broker(
        self,
        order: Order,
        broker_order: Order,
        report: RecoveryReport,
    ) -> None:
        """券商有此订单 → 以券商状态为真值同步。"""
        cid = str(order.client_order_id)
        target = broker_order.status
        synced_filled: Decimal | None = None

        # 同步 broker_order_id(可能本地还是 None)
        if broker_order.broker_order_id and not order.broker_order_id:
            order.broker_order_id = broker_order.broker_order_id

        # 同步成交数量 / 均价
        if broker_order.filled_quantity != order.filled_quantity:
            synced_filled = broker_order.filled_quantity
            order.filled_quantity = broker_order.filled_quantity
            order.average_fill_price = broker_order.average_fill_price

        old_status = order.status

        if old_status == target:
            # 状态一致,仅同步成交数量
            if synced_filled is not None:
                order.touch()
                await self._orders.update_state(order)
                await self._audit_repair(
                    order, old_status, target, "同步成交数量", synced_filled
                )
                report.repairs.append(
                    OrderRepair(
                        client_order_id=cid,
                        from_status=old_status,
                        to_status=target,
                        reason="同步成交数量",
                        synced_filled_quantity=synced_filled,
                    )
                )
            return

        if not OrderStateMachine.can_transition(old_status, target):
            report.unresolvable.append(cid)
            logger.error(
                "recovery.illegal_transition",
                client_order_id=cid,
                from_status=old_status.value,
                to_status=target.value,
            )
            return

        OrderStateMachine.check_transition(old_status, target)
        order.status = target
        order.touch()
        await self._orders.update_state(order)
        await self._audit_repair(order, old_status, target, "券商状态同步", synced_filled)
        report.repairs.append(
            OrderRepair(
                client_order_id=cid,
                from_status=old_status,
                to_status=target,
                reason=f"券商状态同步: {target.value}",
                synced_filled_quantity=synced_filled,
            )
        )
        logger.info(
            "recovery.repaired_from_broker",
            client_order_id=cid,
            from_status=old_status.value,
            to_status=target.value,
        )

    # ------------------------------------------------------------------ 审计
    async def _audit_repair(
        self,
        order: Order,
        old_status: OrderStatus,
        new_status: OrderStatus,
        action: str,
        synced_filled: Decimal | None = None,
    ) -> None:
        detail = f"{old_status.value} → {new_status.value}: {action}"
        if synced_filled is not None:
            detail += f" (filled={synced_filled})"
        await self._audit.add(
            actor="system",
            action="recovery_repair",
            target=str(order.client_order_id),
            payload=detail,
        )
