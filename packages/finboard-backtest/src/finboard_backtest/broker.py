"""``BacktestBroker`` —— 纸面撮合引擎。

根据当前 Bar 的 OHLC 自动撮合订单:

* **市价单**:以当前 Bar ``close`` 成交(可配滑点);
* **限价单**:当 Bar ``low <= price <= high`` 时以 ``limit_price`` 成交;
* **佣金**:``max(commission_rate * turnover, commission_min)``;
* **印花税**:卖出收取 ``stamp_tax_rate * turnover``;
* **手数取整**:下单数量向下取整到 ``lot_size`` 的整数倍;
* **T+1**:买入当日不可卖(可配置关闭)。

BacktestBroker 实现 ``BrokerAdapter`` 接口,但**不**走实盘的 OrderManager → DB 链路;
它由 ``BacktestContext`` 直接调用,所有状态在内存中维护。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import date
from decimal import Decimal
from itertools import count

import structlog

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Bar, Fill, Order, Position
from finboard_shared.types import BrokerKind, OrderStatus, OrderType, RejectReason, Side

logger = structlog.get_logger(__name__)


class BacktestBroker(BrokerAdapter):
    """纸面撮合 broker — 内存级,无 IO。"""

    def __init__(
        self,
        *,
        initial_capital: Decimal = Decimal("100000"),
        commission_rate: Decimal = Decimal("0.0003"),
        commission_min: Decimal = Decimal("5"),
        stamp_tax_rate: Decimal = Decimal("0.0005"),
        slippage_bps: Decimal = Decimal("0"),
        lot_size: int = 100,
        allow_short: bool = False,
        enforce_t_plus_1: bool = True,
    ) -> None:
        self._cash: Decimal = initial_capital
        self._initial_capital = initial_capital
        self._commission_rate = commission_rate
        self._commission_min = commission_min
        self._stamp_tax_rate = stamp_tax_rate
        self._slippage = slippage_bps / Decimal("10000")
        self._lot_size = lot_size
        self._allow_short = allow_short
        self._enforce_t1 = enforce_t_plus_1

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._pending_limit: dict[str, Order] = {}
        self._current_bars: dict[str, Bar] = {}
        self._current_date: date | None = None

        # T+1: symbol_code → 最近一次买入日期
        self._t1_buy_date: dict[str, date] = {}

        self._event_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._id_counter = count(1)
        self._account_id = AccountId("backtest")
        self._connected = False

    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.BACKTEST

    # ------------------------------------------------------------------ 连接

    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        self._account_id = account_id
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 查询

    async def query_account(self) -> Account:
        return Account(
            account_id=self._account_id,
            broker_kind=BrokerKind.BACKTEST,
            total_asset=self.total_equity(),
            cash=self._cash,
            frozen_cash=Decimal("0"),
        )

    async def query_positions(self) -> list[Position]:
        return [self._snapshot_position(code) for code in self._positions]

    async def query_order(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None:
            return None
        return self._clone_order(order)

    def query_order_sync(self, client_order_id: str) -> Order | None:
        """同步查单(供 BacktestEngine 在事件处理中调用)。"""
        order = self._orders.get(client_order_id)
        if order is None:
            return None
        return self._clone_order(order)

    async def query_active_orders(self) -> list[Order]:
        return [
            self._clone_order(o)
            for o in self._orders.values()
            if not o.is_terminal
        ]

    # ------------------------------------------------------------------ 交易

    async def place_order(self, order: Order) -> SubmissionResult:
        cid = str(order.client_order_id)
        self._orders[cid] = order

        # 手数取整
        rounded = self._round_lot(order.quantity)
        if rounded < self._lot_size:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.BROKER_REJECTED
            order.reject_message = (
                f"数量 {order.quantity} 不足 {self._lot_size} 股(1 手)"
            )
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=RejectReason.BROKER_REJECTED,
            )
        order.quantity = rounded

        bar = self._current_bars.get(order.symbol.code)
        if bar is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.BROKER_REJECTED
            order.reject_message = f"无 {order.symbol.code} 的行情数据"
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=RejectReason.BROKER_REJECTED,
            )

        if order.order_type == OrderType.MARKET:
            self._fill_order(order, bar.close)
            return SubmissionResult(
                client_order_id=order.client_order_id,
                broker_order_id=str(next(self._id_counter)),
                accepted=True,
            )

        if order.order_type == OrderType.LIMIT:
            if self._can_fill_limit(order, bar):
                fill_price = order.price or bar.close
                self._fill_order(order, fill_price)
            else:
                order.status = OrderStatus.ACKNOWLEDGED
                order.broker_order_id = str(next(self._id_counter))
                self._pending_limit[cid] = order
            return SubmissionResult(
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                accepted=True,
            )

        order.status = OrderStatus.REJECTED
        return SubmissionResult(
            client_order_id=order.client_order_id,
            accepted=False,
            reject_reason=RejectReason.BROKER_REJECTED,
        )

    async def cancel_order(self, client_order_id: str) -> None:
        order = self._pending_limit.pop(client_order_id, None)
        if order is not None:
            order.status = OrderStatus.CANCELLED

    # ------------------------------------------------------------------ 回报流

    def events(self) -> AsyncIterator[BrokerEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[BrokerEvent]:
        while self._connected or not self._event_queue.empty():
            event = await self._event_queue.get()
            yield event

    async def drain_events(self) -> list[BrokerEvent]:
        """取出当前缓冲的所有事件(供 BacktestEngine 同步消费)。"""
        events: list[BrokerEvent] = []
        while not self._event_queue.empty():
            events.append(self._event_queue.get_nowait())
        return events

    # ------------------------------------------------------------------ 回测驱动

    def on_new_bar(self, bar: Bar) -> None:
        """每根新 Bar 到达时调用:更新行情 + 撮合挂起的限价单。"""
        self._current_bars[bar.symbol.code] = bar
        self._current_date = bar.timestamp.date()

        filled_cids: list[str] = []
        for cid, order in list(self._pending_limit.items()):
            od = self._current_bars.get(order.symbol.code)
            if od and od.symbol.code == bar.symbol.code and self._can_fill_limit(order, od):
                fill_price = order.price or bar.close
                self._fill_order(order, fill_price)
                filled_cids.append(cid)

        for cid in filled_cids:
            self._pending_limit.pop(cid, None)

    def total_equity(self) -> Decimal:
        pos_value = Decimal("0")
        for code, pos in self._positions.items():
            bar = self._current_bars.get(code)
            price = bar.close if bar else pos.average_price
            pos_value += pos.total_quantity * price
        return self._cash + pos_value

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    @property
    def all_orders(self) -> list[Order]:
        return [self._clone_order(o) for o in self._orders.values()]

    # ------------------------------------------------------------------ 内部

    def _round_lot(self, quantity: Decimal) -> Decimal:
        lots = quantity // self._lot_size
        return lots * self._lot_size

    @staticmethod
    def _can_fill_limit(order: Order, bar: Bar) -> bool:
        price = order.price
        if price is None:
            return False
        if order.side is Side.BUY:
            return bar.low <= price
        return bar.high >= price

    def _fill_order(self, order: Order, raw_price: Decimal) -> None:
        """执行成交:更新现金 / 持仓 / 订单状态,推入事件队列。"""
        # 滑点
        if order.side is Side.BUY:
            fill_price = raw_price * (Decimal("1") + self._slippage)
        else:
            fill_price = raw_price * (Decimal("1") - self._slippage)

        qty = order.quantity
        turnover = fill_price * qty

        # 佣金
        commission = max(turnover * self._commission_rate, self._commission_min)
        # 印花税(仅卖出)
        tax = turnover * self._stamp_tax_rate if order.side is Side.SELL else Decimal("0")

        total_cost = commission + tax

        # T+1 检查(卖出)
        if order.side is Side.SELL and self._enforce_t1:
            code = order.symbol.code
            buy_date = self._t1_buy_date.get(code)
            if buy_date is not None and self._current_date == buy_date:
                order.status = OrderStatus.REJECTED
                order.reject_reason = RejectReason.BROKER_REJECTED
                order.reject_message = f"T+1: {code} 当日买入不可卖"
                self._push_event(BrokerEventType.ORDER_REJECTED, order)
                return

        # 现金变动
        if order.side is Side.BUY:
            needed = turnover + total_cost
            if self._cash < needed and not self._allow_short:
                order.status = OrderStatus.REJECTED
                order.reject_reason = RejectReason.INSUFFICIENT_CASH
                order.reject_message = f"资金不足: 需 {needed}, 有 {self._cash}"
                self._push_event(BrokerEventType.ORDER_REJECTED, order)
                return
            self._cash -= needed
        else:
            received = turnover - total_cost
            self._cash += received

        # 更新持仓
        self._update_position(order, qty, fill_price)

        # 记录成交
        fill = Fill(
            fill_id=f"BT-{next(self._id_counter)}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=fill_price,
            commission=commission,
            tax=tax,
            broker_order_id=order.broker_order_id,
        )
        self._fills.append(fill)

        # 更新订单状态
        order.status = OrderStatus.FILLED
        order.filled_quantity = qty
        order.average_fill_price = fill_price

        self._push_event(
            BrokerEventType.ORDER_FILLED,
            order,
            fill=fill,
        )

    def _update_position(
        self, order: Order, qty: Decimal, price: Decimal
    ) -> None:
        code = order.symbol.code
        pos = self._positions.get(code)
        if pos is None:
            pos = Position(
                account_id=self._account_id,
                symbol=order.symbol,
            )
            self._positions[code] = pos

        if order.side is Side.BUY:
            old_value = pos.total_quantity * pos.average_price
            new_value = qty * price
            total_qty = pos.total_quantity + qty
            if total_qty > 0:
                pos.average_price = (old_value + new_value) / total_qty
            pos.total_quantity = total_qty
            pos.available_quantity = total_qty
            # T+1: 记录买入日期
            if self._enforce_t1 and self._current_date is not None:
                self._t1_buy_date[code] = self._current_date
        else:
            pos.total_quantity -= qty
            if pos.total_quantity <= 0:
                pos.total_quantity = Decimal("0")
                pos.average_price = Decimal("0")
            pos.available_quantity = pos.total_quantity

        pos.touch()

    def _snapshot_position(self, code: str) -> Position:
        pos = self._positions[code]
        bar = self._current_bars.get(code)
        mv = Decimal("0")
        unrealized = Decimal("0")
        if bar and pos.total_quantity > 0:
            mv = pos.total_quantity * bar.close
            unrealized = mv - pos.total_quantity * pos.average_price
        return Position(
            account_id=pos.account_id,
            symbol=pos.symbol,
            position_side=pos.position_side,
            total_quantity=pos.total_quantity,
            available_quantity=pos.available_quantity,
            frozen_quantity=pos.frozen_quantity,
            average_price=pos.average_price,
            market_value=mv,
            unrealized_pnl=unrealized,
        )

    def _clone_order(self, order: Order) -> Order:
        from dataclasses import replace

        return replace(order)

    def _push_event(
        self,
        event_type: BrokerEventType,
        order: Order,
        fill: Fill | None = None,
    ) -> None:
        self._event_queue.put_nowait(
            BrokerEvent(
                type=event_type,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                fill=fill,
                reject_reason=order.reject_reason,
                message=order.reject_message or "",
            )
        )
