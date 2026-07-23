"""``OrderManager`` —— 订单全生命周期的中枢。

职责:
1. 接收 ``OrderRequest``,生成 ``client_order_id`` 并构造领域 ``Order``;
2. 调用注入的 :class:`RiskChecker`,通过则进入 ``RISK_CHECKED``;
3. 持久化(``OrderRepository.add``),DB UNIQUE 是重复下单的最后一道兜底;
4. 提交 broker(``BrokerAdapter.place_order``);
5. 处理 broker 推送的回报事件(委托确认 / 成交 / 拒单 / 撤单);
6. 在每次状态变化后发布对应的 :mod:`finboard_core.events` 给 EventBus。

**红线**(见 AGENTS.md):
* ``client_order_id`` 由本地生成,不接受外部传入;
* 下单超时 → 置 ``UNKNOWN`` → 查券商确认不存在后才允许用新 ID 重发;
* 状态迁移全部经 :class:`OrderStateMachine.check_transition`。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_core.bus import EventBus
from finboard_core.events import (
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from finboard_core.protocols import RiskChecker
from finboard_core.state_machine import OrderStateMachine
from finboard_persistence.repo import (
    AuditLogRepository,
    FillRepository,
    OrderRepository,
)
from finboard_shared.exceptions import BrokerTimeoutError, KernelNotReadyError
from finboard_shared.identifiers import AccountId, ClientOrderId, generate_client_order_id
from finboard_shared.models import Order, OrderRequest
from finboard_shared.types import (
    BrokerKind,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)

logger = structlog.get_logger(__name__)


class OrderManager:
    """订单管理器(单账户 P0 范围)。"""

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        order_repo: OrderRepository,
        fill_repo: FillRepository,
        audit_repo: AuditLogRepository,
        risk_checker: RiskChecker,
        event_bus: EventBus,
        account_id: AccountId,
        gate: Callable[[], bool] | None = None,
    ) -> None:
        self._broker = broker
        self._orders = order_repo
        self._fills = fill_repo
        self._audit = audit_repo
        self._risk = risk_checker
        self._bus = event_bus
        self._account_id = account_id
        # 内核就绪 gate —— 核对通过前禁止下单(交易安全红线)。
        # 由 TradingKernel 注入(lambda 读 kernel._ready);None 时跳过检查
        # (向后兼容,如独立单测 OrderManager)。
        self._gate = gate
        # 内存活动订单缓存,减少高频回报时的 DB 查询
        self._inflight: dict[str, Order] = {}
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._consumer_task is not None:
            return
        # 把 DB 中的活动订单加载到内存缓存(重启恢复 P0 后期完善)
        active = await self._orders.list_active(str(self._account_id))
        for order in active:
            self._inflight[str(order.client_order_id)] = order
        self._consumer_task = asyncio.create_task(
            self._consume_broker_events(), name="broker-event-consumer"
        )
        logger.info(
            "order_manager.started",
            account_id=str(self._account_id),
            active_orders=len(self._inflight),
        )

    async def stop(self) -> None:
        if self._consumer_task is None:
            return
        self._consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._consumer_task
        self._consumer_task = None

    # ------------------------------------------------------------------ 下单
    async def place_order(self, request: OrderRequest) -> Order:
        """完整下单链路:就绪检查 → 风控 → 入库 → 提交 broker → 状态推进。"""
        # 交易安全红线:核对通过前禁止下单(kernel.start 的 reconcile gate)
        if self._gate is not None and not self._gate():
            raise KernelNotReadyError(
                "交易内核未就绪(核对未通过或未完成),禁止下单"
            )
        if str(request.account_id) != str(self._account_id):
            raise ValueError(
                f"OrderRequest.account_id({request.account_id}) 与 OrderManager.account_id"
                f"({self._account_id}) 不一致"
            )

        # 1) Kill Switch 软探测 + 风控规则
        await self._risk.check(request)

        # 2) 生成 client_order_id + 构造 Order
        client_order_id = generate_client_order_id()
        order = Order(
            client_order_id=client_order_id,
            account_id=self._account_id,
            broker_kind=self._broker.kind,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            strategy_id=request.strategy_id,
            price=request.price,
            time_in_force=request.time_in_force,
            position_side=request.position_side,
            status=OrderStatus.RISK_CHECKED,
            risk_checked_at=datetime.now(UTC),
        )

        # 3) DB 落地(UNIQUE 索引兜底防重复)
        await self._orders.add(order)
        self._inflight[str(client_order_id)] = order
        await self._bus.publish(OrderCreated(order=order))

        # 4) 推进到 SUBMITTING
        await self._transition(order, OrderStatus.SUBMITTING)

        # 5) 提交 broker(超时走 UNKNOWN 红线)
        submission = await self._submit_to_broker(order)
        if not submission.accepted:
            await self._reject(
                order,
                reason=submission.reject_reason or RejectReason.BROKER_REJECTED,
                message="broker 立即拒单",
            )
            return order

        # 6) broker 同步 ACK → ACKNOWLEDGED;否则停在 SUBMITTED 等回报
        if submission.broker_order_id:
            order.broker_order_id = submission.broker_order_id
            await self._transition(order, OrderStatus.SUBMITTED)
            await self._transition(order, OrderStatus.ACKNOWLEDGED)
        else:
            await self._transition(order, OrderStatus.SUBMITTED)
        await self._orders.update_state(order)
        await self._audit.add(
            actor=request.strategy_id or "manual",
            action="place_order",
            target=str(order.client_order_id),
            payload=f"{order.side.value} {order.quantity} {order.symbol.code}@{order.price or 'mkt'} -> {order.status.value}",
        )
        await self._bus.publish(OrderSubmitted(order=order))
        return order

    async def cancel_order(self, client_order_id: str) -> None:
        order = self._inflight.get(client_order_id)
        if order is None:
            order = await self._orders.get(client_order_id)
            if order is None:
                raise KeyError(f"未找到订单 {client_order_id}")
            self._inflight[client_order_id] = order
        if not order.is_active:
            raise ValueError(f"订单 {client_order_id} 已不可撤,状态 {order.status.value}")
        await self._transition(order, OrderStatus.CANCEL_PENDING)
        await self._orders.update_state(order)
        await self._audit.add(
            actor="system",
            action="cancel_order",
            target=client_order_id,
            payload=f"status={order.status.value}",
        )
        # 撤单同样适用超时不重试红线;此处由 broker 内部抛 BrokerTimeoutError
        try:
            await self._broker.cancel_order(client_order_id)
        except BrokerTimeoutError:
            # 不重试,等待回报或人工介入
            await self._audit.add(
                actor="system",
                action="cancel_timeout",
                target=client_order_id,
                payload=f"status={order.status.value}",
            )
            logger.warning(
                "order_manager.cancel_timeout",
                client_order_id=client_order_id,
            )
            raise

    # ------------------------------------------------------------------ 批量撤单
    async def cancel_all_active(self) -> list[str]:
        """撤销所有活动订单(收盘撤单 / Kill Switch CANCEL_ALL 调用)。

        采用两阶段撤单避免 "Session is already flushing" 竞争:
        1. 阶段一:把所有活动订单 transition 到 CANCEL_PENDING + 单次 flush
        2. 阶段二:逐个调用 broker.cancel_order(事件由 consumer 异步处理)

        这样 consumer 处理 CANCELLED 事件时的 flush 不会与我们的 flush 竞争,
        因为阶段一 flush 完成后才进入阶段二。

        Returns:
            成功提交撤单请求的 ``client_order_id`` 列表。
        """
        active = await self._orders.list_active(str(self._account_id))
        candidates: list[tuple[str, Order]] = []
        for order in active:
            if not order.is_active:
                continue
            candidates.append((str(order.client_order_id), order))

        if not candidates:
            return []

        # 阶段一:批量状态转换(单次 flush,避免与 consumer 竞争)
        for cid, order in candidates:
            await self._transition(order, OrderStatus.CANCEL_PENDING)
            await self._orders.update_state(order)
            await self._audit.add(
                actor="system",
                action="cancel_order",
                target=cid,
                payload=f"status={order.status.value}",
            )

        cancelled: list[str] = []
        # 阶段二:逐个发 broker 撤单请求
        for cid, order in candidates:
            try:
                await self._broker.cancel_order(cid)
                cancelled.append(cid)
            except BrokerTimeoutError:
                await self._audit.add(
                    actor="system",
                    action="cancel_timeout",
                    target=cid,
                    payload=f"status={order.status.value}",
                )
                logger.warning(
                    "order_manager.cancel_all_active_timeout",
                    client_order_id=cid,
                )
            except Exception:
                logger.exception(
                    "order_manager.cancel_all_active_failed",
                    client_order_id=cid,
                    status=order.status.value,
                )

        if cancelled:
            await self._audit.add(
                actor="system",
                action="cancel_all_active",
                payload=f"cancelled={len(cancelled)} ids={','.join(cancelled)}",
            )
        logger.info(
            "order_manager.cancel_all_active_done",
            total_active=len(active),
            cancelled=len(cancelled),
        )
        return cancelled

    # ------------------------------------------------------------------ 回报消费
    async def _consume_broker_events(self) -> None:
        async for event in self._broker.events():
            try:
                await self._handle_broker_event(event)
            except Exception:
                logger.exception(
                    "order_manager.event_handler_failed",
                    event_type=event.type.value,
                    client_order_id=str(event.client_order_id),
                )

    async def _handle_broker_event(self, event: BrokerEvent) -> None:
        if event.type is BrokerEventType.ORDER_ACCEPTED:
            await self._on_accepted(event)
        elif event.type is BrokerEventType.ORDER_FILLED:
            await self._on_filled(event)
        elif event.type is BrokerEventType.ORDER_CANCELLED:
            await self._on_cancelled(event)
        elif event.type is BrokerEventType.ORDER_REJECTED:
            await self._on_rejected(event)
        elif event.type in (
            BrokerEventType.CONNECTED,
            BrokerEventType.DISCONNECTED,
            BrokerEventType.ERROR,
        ):
            logger.info(
                "order_manager.broker_session_event", event_type=event.type.value
            )

    async def _on_accepted(self, event: BrokerEvent) -> None:
        if event.client_order_id is None:
            return
        order = await self._get_order_inflight(event.client_order_id)
        if order is None:
            return
        if event.broker_order_id and order.broker_order_id is None:
            order.broker_order_id = event.broker_order_id
        if order.status in (OrderStatus.SUBMITTING, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
            await self._transition(order, OrderStatus.ACKNOWLEDGED)
            order.acknowledged_at = datetime.now(UTC)
            await self._orders.update_state(order)

    async def _on_filled(self, event: BrokerEvent) -> None:
        if event.fill is None or event.client_order_id is None:
            return
        fill = event.fill
        # 幂等:同一 fill_id 的回报可能被 broker 在重连后重放;若已入库,
        # 跳过累加(否则 order.filled_quantity 会双写)。
        if await self._fills.get(fill.fill_id) is not None:
            logger.info(
                "order_manager.duplicate_fill_ignored", fill_id=fill.fill_id
            )
            return
        order = await self._get_order_inflight(event.client_order_id)
        if order is None:
            return
        await self._fills.add(fill)
        # 更新订单聚合字段
        order.filled_quantity += fill.quantity
        if order.average_fill_price is None:
            order.average_fill_price = fill.price
        else:
            order.average_fill_price = (
                (order.average_fill_price * (order.filled_quantity - fill.quantity))
                + (fill.price * fill.quantity)
            ) / order.filled_quantity
        target = (
            OrderStatus.FILLED
            if order.remaining_quantity <= Decimal("0")
            else OrderStatus.PARTIALLY_FILLED
        )
        await self._transition(order, target)
        await self._orders.update_state(order)
        await self._audit.add(
            actor="system",
            action="order_filled",
            target=str(fill.client_order_id),
            payload=f"fill_id={fill.fill_id} qty={fill.quantity} price={fill.price} status={order.status.value}",
        )
        await self._bus.publish(OrderFilled(order=order, fill=fill))

    async def _on_cancelled(self, event: BrokerEvent) -> None:
        if event.client_order_id is None:
            return
        order = await self._get_order_inflight(event.client_order_id)
        if order is None:
            return
        await self._transition(order, OrderStatus.CANCELLED)
        await self._orders.update_state(order)
        await self._bus.publish(OrderCancelled(order=order))

    async def _on_rejected(self, event: BrokerEvent) -> None:
        if event.client_order_id is None:
            return
        order = await self._get_order_inflight(event.client_order_id)
        if order is None:
            return
        await self._reject(
            order,
            reason=event.reject_reason or RejectReason.BROKER_REJECTED,
            message=event.message,
        )

    # ------------------------------------------------------------------ 内部
    async def _submit_to_broker(self, order: Order) -> SubmissionResult:
        """调用 broker 下单;严格处理超时红线。"""
        try:
            return await self._broker.place_order(order)
        except BrokerTimeoutError:
            # **红线**:超时后立即置 UNKNOWN,不重发;等待查询驱动恢复
            await self._transition(order, OrderStatus.UNKNOWN)
            order.submitted_at = datetime.now(UTC)
            await self._orders.update_state(order)
            await self._audit.add(
                actor="system",
                action="submit_timeout",
                target=str(order.client_order_id),
                payload=f"broker={order.broker_kind.value}",
            )
            logger.error(
                "order_manager.submit_timeout_unknown",
                client_order_id=str(order.client_order_id),
                broker=order.broker_kind.value,
            )
            raise
        except Exception:
            await self._transition(order, OrderStatus.REJECTED)
            order.reject_reason = RejectReason.BROKER_REJECTED
            await self._orders.update_state(order)
            raise

    async def _transition(self, order: Order, to: OrderStatus) -> None:
        OrderStateMachine.check_transition(order.status, to)
        order.status = to
        order.touch()

    async def _reject(
        self, order: Order, *, reason: RejectReason, message: str
    ) -> None:
        await self._transition(order, OrderStatus.REJECTED)
        order.reject_reason = reason
        order.reject_message = message
        await self._orders.update_state(order)
        await self._audit.add(
            actor="system",
            action="order_rejected",
            target=str(order.client_order_id),
            payload=f"reason={reason.value} msg={message}",
        )
        await self._bus.publish(OrderRejected(order=order, reason=reason, message=message))

    async def _get_order_inflight(self, client_order_id: ClientOrderId) -> Order | None:
        key = str(client_order_id)
        order = self._inflight.get(key)
        if order is not None:
            return order
        db_order = await self._orders.get(key)
        if db_order is not None:
            self._inflight[key] = db_order
            return db_order
        return None

    # ------------------------------------------------------------------ 占位
    _ = (BrokerKind, OrderType, Side)  # 防止未使用告警
