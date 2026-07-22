"""Mock Broker —— 内存级实现,用于本地开发与 CI。

特性:
* 限价单挂起,直到调用 :meth:`MockBroker.match_limit_order` 手工撮合
  或对冲市价单触发;
* 市价单立即按调用方提供的 ``order.price`` 或默认价成交;
* 撤单立即生效;
* 持仓 / 资金按成交事件就地更新,行为接近真实券商(不含手续费,可配置);
* 所有状态变化通过 ``events()`` 流推送给 OrderManager。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.exceptions import BrokerError, OrderNotFoundError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Fill, Order, Position
from finboard_shared.types import BrokerKind, OrderStatus, OrderType, Side


class MockBroker(BrokerAdapter):
    """所有状态保存在实例字段中,多次测试间应使用新实例。"""

    def __init__(
        self,
        *,
        initial_cash: Decimal = Decimal("1_000_000"),
        initial_positions: Mapping[str, Position] | None = None,
        commission_rate: Decimal = Decimal("0.0003"),
    ) -> None:
        self._connected: bool = False
        self._account_id: AccountId | None = None
        self._cash: Decimal = initial_cash
        self._positions: dict[str, Position] = {
            code: replace(p) for code, p in (initial_positions or {}).items()
        }
        self._orders: dict[str, Order] = {}  # key: client_order_id(string)
        # broker 视角的状态映射 —— **不**通过修改传入 Order.status 来维护
        # (status 是 OrderManager 的职责);本表只供 query_* 返回正确 broker 状态。
        self._broker_status: dict[str, OrderStatus] = {}
        # broker 视角的成交聚合 —— 同样不污染传入 order.filled_quantity
        # (否则 OrderManager._on_filled 会再累加一次,导致双写)。query 时通过
        # _snapshot 覆盖返回。
        self._broker_filled_qty: dict[str, Decimal] = {}
        self._broker_avg_price: dict[str, Decimal | None] = {}
        self._fills: list[Fill] = []
        self._commission_rate = commission_rate
        self._next_id: int = 1
        self._events_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._closed: bool = False
        # 持有后台 fill task 的强引用,避免 GC 中途回收
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ 元数据
    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.MOCK

    # ------------------------------------------------------------------ 连接
    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        if self._connected:
            return
        self._account_id = account_id
        self._connected = True
        await self._push(BrokerEvent(type=BrokerEventType.CONNECTED, message=str(account_id)))

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._closed = True
        await self._push(BrokerEvent(type=BrokerEventType.DISCONNECTED))

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 查询
    async def query_account(self) -> Account:
        self._require_connected()
        assert self._account_id is not None
        total_asset = self._cash + sum(
            (p.total_quantity * p.average_price for p in self._positions.values()),
            Decimal("0"),
        )
        return Account(
            account_id=self._account_id,
            broker_kind=self.kind,
            total_asset=total_asset,
            cash=self._cash,
        )

    async def query_positions(self) -> list[Position]:
        self._require_connected()
        return [replace(p) for p in self._positions.values()]

    async def query_active_orders(self) -> list[Order]:
        self._require_connected()
        # 以 broker 视角状态为准,过滤出仍活动的订单
        return [
            self._snapshot(cid)
            for cid, status in self._broker_status.items()
            if status.is_active
        ]

    async def query_order(self, client_order_id: str) -> Order | None:
        self._require_connected()
        if client_order_id not in self._orders:
            return None
        return self._snapshot(client_order_id)

    # ------------------------------------------------------------------ 交易
    async def place_order(self, order: Order) -> SubmissionResult:
        self._require_connected()
        cid = str(order.client_order_id)
        if cid in self._orders:
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=None,
            )

        broker_order_id = f"M-{self._next_id:08d}"
        self._next_id += 1

        # 关键契约:不修改传入 Order 的 status / acknowledged_at —— 状态推进
        # 是 OrderManager 的职责,本方法只回 SubmissionResult + 推事件。
        # 仅允许写 broker_order_id(OrderManager 后续会读它填回 DB)。
        order.broker_order_id = broker_order_id
        self._orders[cid] = order
        # broker 视角:订单已被接受
        self._broker_status[cid] = OrderStatus.ACKNOWLEDGED

        await self._push(
            BrokerEvent(
                type=BrokerEventType.ORDER_ACCEPTED,
                client_order_id=order.client_order_id,
                broker_order_id=broker_order_id,
            )
        )

        # 市价单立即成交(用 order.price 或一个安全默认价)
        if order.order_type is OrderType.MARKET:
            task = asyncio.create_task(
                self._fill_order(order, order.price or Decimal("100"))
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return SubmissionResult(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            accepted=True,
        )

    async def cancel_order(self, client_order_id: str) -> None:
        self._require_connected()
        cid = str(client_order_id)
        if cid not in self._orders:
            raise OrderNotFoundError(f"mock broker 找不到订单 {client_order_id}")
        status = self._broker_status.get(cid, OrderStatus.CREATED)
        if not status.is_active:
            raise BrokerError(f"订单 {client_order_id} 不可撤,当前状态 {status.value}")
        self._broker_status[cid] = OrderStatus.CANCEL_PENDING
        await self._push(
            BrokerEvent(
                type=BrokerEventType.ORDER_CANCELLED,
                client_order_id=self._orders[cid].client_order_id,
                broker_order_id=self._orders[cid].broker_order_id,
            )
        )
        self._broker_status[cid] = OrderStatus.CANCELLED

    # ------------------------------------------------------------------ 事件流
    def events(self) -> AsyncIterator[BrokerEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[BrokerEvent]:
        while not self._closed or not self._events_queue.empty():
            event = await self._events_queue.get()
            yield event

    # ------------------------------------------------------------------ 测试辅助
    async def match_limit_order(
        self, client_order_id: str, price: Decimal
    ) -> None:
        """手工撮合一笔挂着的限价单,触发 ``ORDER_FILLED`` 事件。"""
        cid = str(client_order_id)
        if cid not in self._orders:
            raise OrderNotFoundError(client_order_id)
        status = self._broker_status.get(cid, OrderStatus.CREATED)
        if not status.is_active:
            raise BrokerError(f"订单 {client_order_id} 已不可撮合,状态 {status.value}")
        await self._fill_order(self._orders[cid], price)

    # ------------------------------------------------------------------ 内部
    def _snapshot(self, cid: str) -> Order:
        """以 broker 视角状态构造一份 Order 副本(query_* 用)。

        强调:返回的是 **replace 后的副本**,调用方修改不会影响内部状态;
        同时把 broker 视角的 status / filled_quantity / average_fill_price
        覆盖进去,确保 query 结果反映 broker 侧最新进展。
        """
        base = self._orders[cid]
        return replace(
            base,
            status=self._broker_status.get(cid, base.status),
            filled_quantity=self._broker_filled_qty.get(cid, Decimal("0")),
            average_fill_price=self._broker_avg_price.get(cid),
            acknowledged_at=base.acknowledged_at,
        )

    async def _fill_order(self, order: Order, price: Decimal) -> None:
        assert self._account_id is not None
        cid = str(order.client_order_id)
        qty = order.remaining_quantity
        commission = (qty * price * self._commission_rate).quantize(Decimal("0.01"))

        fill = Fill(
            fill_id=f"F-{self._next_id:08d}",
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=price,
            commission=commission,
        )
        self._next_id += 1

        # 成交聚合字段只更新 broker 视角映射,**不**修改传入 order —— 否则
        # OrderManager._on_filled 会再 += 一次 fill.quantity 导致双写。
        # query_* 返回的值由 _snapshot 从这些映射覆盖。
        new_filled = self._broker_filled_qty.get(cid, Decimal("0")) + qty
        self._broker_filled_qty[cid] = new_filled
        prev_avg = self._broker_avg_price.get(cid)
        if prev_avg is None:
            self._broker_avg_price[cid] = price
        else:
            prev_qty = new_filled - qty
            self._broker_avg_price[cid] = (
                prev_avg * prev_qty + price * qty
            ) / new_filled
        self._broker_status[cid] = (
            OrderStatus.FILLED
            if order.quantity - new_filled <= 0
            else OrderStatus.PARTIALLY_FILLED
        )
        self._fills.append(fill)

        self._apply_fill_to_account(fill, order.side)
        await self._push(
            BrokerEvent(
                type=BrokerEventType.ORDER_FILLED,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                fill=fill,
            )
        )

    def _apply_fill_to_account(self, fill: Fill, side: Side) -> None:
        """简化版持仓/资金维护:不区分多空、不计手续费扣除。

        真实账户维护在 ``finboard_core.PositionManager`` 中完成,Mock 仅保证
        查询接口返回的状态能反映最新成交。
        """
        pos = self._positions.get(fill.symbol.code)
        if pos is None:
            assert self._account_id is not None
            pos = Position.empty(self._account_id, fill.symbol)
            self._positions[fill.symbol.code] = pos

        if side is Side.BUY:
            new_total = pos.total_quantity + fill.quantity
            if new_total > 0:
                pos.average_price = (
                    pos.total_quantity * pos.average_price + fill.quantity * fill.price
                ) / new_total
            pos.total_quantity = new_total
            pos.available_quantity = new_total  # 简化:T+0 可卖
            self._cash -= fill.quantity * fill.price + fill.commission
        else:
            pos.total_quantity -= fill.quantity
            pos.available_quantity = max(
                Decimal("0"), pos.available_quantity - fill.quantity
            )
            self._cash += fill.quantity * fill.price - fill.commission

        pos.market_value = pos.total_quantity * pos.average_price
        pos.updated_at = datetime.now(UTC)

    async def _push(self, event: BrokerEvent) -> None:
        await self._events_queue.put(event)

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerError("MockBroker 未连接")
