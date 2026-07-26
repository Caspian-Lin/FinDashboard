"""StrategyRunner 单元测试 —— MockMarketData + 手工构造 OrderManager 的 mock。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from finboard_broker import MockMarketData
from finboard_core import EventBus
from finboard_core.events import (
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from finboard_core.strategy import (
    OrderEvent,
    Strategy,
    StrategyContext,
    StrategyRunner,
    TimerEvent,
)
from finboard_shared.identifiers import AccountId, ClientOrderId, StrategyId
from finboard_shared.models import Fill, Order, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, RejectReason, Side

ACCOUNT = AccountId("test-account")
SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


def _make_order(
    *,
    strategy_id: StrategyId | None = None,
    status: OrderStatus = OrderStatus.ACKNOWLEDGED,
) -> Order:
    return Order(
        client_order_id=ClientOrderId("F-20250101-deadbeefcafebabe"),
        account_id=ACCOUNT,
        broker_kind="mock",  # type: ignore[arg-type]
        symbol=SYMBOL,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("100"),
        strategy_id=strategy_id,
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def runner() -> StrategyRunner:
    """构造一个带 MockMarketData 的 StrategyRunner(OrderManager 等用 mock)。"""
    order_mgr = MagicMock()
    order_mgr.place_order = AsyncMock()
    order_mgr.cancel_order = AsyncMock()
    pos_mgr = MagicMock()
    pos_mgr.list_local = AsyncMock(return_value=[])
    acct_mgr = MagicMock()
    acct_mgr.current = AsyncMock(return_value=None)

    md = MockMarketData()
    return StrategyRunner(
        event_bus=EventBus(),
        order_manager=order_mgr,
        position_manager=pos_mgr,
        account_manager=acct_mgr,
        account_id=ACCOUNT,
        market_adapter=md,
        timer_interval=0.05,
    )


# --------------------------------------------------------------------------- 注册与生命周期


@pytest.mark.unit
async def test_register_and_strategy_count(runner: StrategyRunner) -> None:
    class S1(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("s1")

    class S2(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("s2")

    assert runner.strategy_count == 0
    runner.register(S1())
    runner.register(S2())
    assert runner.strategy_count == 2


@pytest.mark.unit
async def test_start_stop_idempotent(runner: StrategyRunner) -> None:
    await runner.start()
    assert runner._running is True
    await runner.start()  # 二次调用不报错
    await runner.stop()
    assert runner._running is False
    await runner.stop()  # 二次调用不报错


# --------------------------------------------------------------------------- on_start


@pytest.mark.unit
async def test_on_start_called(runner: StrategyRunner) -> None:
    called: list[str] = []

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("test-on-start")

        async def on_start(self, ctx: StrategyContext) -> None:
            called.append("started")

    runner.register(MyStrategy())
    await runner.start()
    assert called == ["started"]
    await runner.stop()


@pytest.mark.unit
async def test_on_start_exception_isolated(runner: StrategyRunner) -> None:
    """一个策略 on_start 抛异常不影响其他策略。"""

    class BadStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("bad")

        async def on_start(self, ctx: StrategyContext) -> None:
            raise RuntimeError("boom")

    started: list[str] = []

    class GoodStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("good")

        async def on_start(self, ctx: StrategyContext) -> None:
            started.append("good")

    runner.register(BadStrategy())
    runner.register(GoodStrategy())
    await runner.start()  # 不抛异常
    assert started == ["good"]
    await runner.stop()


# --------------------------------------------------------------------------- 订单事件路由


@pytest.mark.unit
async def test_order_event_routed_by_strategy_id(runner: StrategyRunner) -> None:
    """只有 strategy_id 匹配的策略才收到 on_order_update。"""
    received: list[OrderEvent] = []

    sid_a = StrategyId("strat-a")
    sid_b = StrategyId("strat-b")

    class StrategyA(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return sid_a

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            received.append(event)

    class StrategyB(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return sid_b

    runner.register(StrategyA())
    runner.register(StrategyB())
    await runner.start()

    order_a = _make_order(strategy_id=sid_a)
    order_b = _make_order(strategy_id=sid_b)

    await runner._bus.publish(OrderCreated(order=order_a))
    await runner._bus.publish(OrderCreated(order=order_b))

    assert len(received) == 1
    assert received[0].kind == "created"
    assert str(received[0].order.strategy_id) == "strat-a"

    await runner.stop()


@pytest.mark.unit
async def test_order_event_with_fill(runner: StrategyRunner) -> None:
    """OrderFilled 事件携带 fill 信息。"""
    received: list[OrderEvent] = []

    sid = StrategyId("fill-test")

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return sid

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            received.append(event)

    runner.register(MyStrategy())
    await runner.start()

    order = _make_order(strategy_id=sid, status=OrderStatus.FILLED)
    fill = Fill(
        fill_id="F-00000001",
        client_order_id=order.client_order_id,
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("100"),
        price=Decimal("4.50"),
        filled_at=datetime.now(UTC),
    )

    await runner._bus.publish(OrderFilled(order=order, fill=fill))

    assert len(received) == 1
    assert received[0].kind == "filled"
    assert received[0].fill is not None
    assert received[0].fill.quantity == Decimal("100")

    await runner.stop()


@pytest.mark.unit
async def test_order_event_no_strategy_id_skipped(runner: StrategyRunner) -> None:
    """没有 strategy_id 的订单事件不路由到任何策略。"""
    received: list[OrderEvent] = []

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("test")

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            received.append(event)

    runner.register(MyStrategy())
    await runner.start()

    order = _make_order(strategy_id=None)
    await runner._bus.publish(OrderCreated(order=order))

    assert len(received) == 0
    await runner.stop()


@pytest.mark.unit
async def test_on_order_update_exception_isolated(runner: StrategyRunner) -> None:
    """策略 on_order_update 抛异常不影响其他策略和内核。"""
    sid = StrategyId("exc-test")
    received: list[str] = []

    class BadStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return sid

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            raise RuntimeError("callback boom")

    class GoodStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("good-2")

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            received.append(event.kind)

    runner.register(BadStrategy())
    runner.register(GoodStrategy())
    await runner.start()

    # Bad 策略的订单事件 → 抛异常
    await runner._bus.publish(
        OrderCreated(order=_make_order(strategy_id=sid))
    )
    # Good 策略的订单事件 → 正常
    good_order = _make_order(strategy_id=StrategyId("good-2"))
    await runner._bus.publish(OrderCreated(order=good_order))

    assert received == ["created"]
    await runner.stop()


# --------------------------------------------------------------------------- 行情事件路由


@pytest.mark.unit
async def test_market_data_routed(runner: StrategyRunner) -> None:
    """行情事件推送到所有策略。"""
    from finboard_broker import MarketDataEventType
    from finboard_shared.models import Tick

    received: list[Any] = []

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("md-test")

        async def on_market_data(self, event, ctx: StrategyContext) -> None:
            received.append(event)

    runner.register(MyStrategy())
    await runner.start()

    tick = Tick(
        symbol=SYMBOL,
        last_price=Decimal("4.50"),
        timestamp=datetime.now(UTC),
    )
    await runner._market_adapter.connect()  # type: ignore[union-attr]
    await runner._market_adapter.subscribe_tick([SYMBOL])  # type: ignore[union-attr]
    await runner._market_adapter.inject_tick(tick)  # type: ignore[union-attr]

    await asyncio.sleep(0.1)

    tick_events = [
        e for e in received if e.type is MarketDataEventType.TICK
    ]
    assert len(tick_events) >= 1
    assert tick_events[0].tick is not None
    assert tick_events[0].tick.last_price == Decimal("4.50")

    await runner.stop()


# --------------------------------------------------------------------------- 定时事件


@pytest.mark.unit
async def test_timer_fires(runner: StrategyRunner) -> None:
    """定时器按 timer_interval 周期触发。"""
    timer_count: list[int] = []

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("timer-test")

        async def on_timer(
            self, event: TimerEvent, ctx: StrategyContext
        ) -> None:
            timer_count.append(1)

    runner.register(MyStrategy())
    await runner.start()

    # timer_interval = 0.05s;等 0.2s 足够触发 2+ 次
    await asyncio.sleep(0.2)
    await runner.stop()

    assert len(timer_count) >= 2


@pytest.mark.unit
async def test_timer_exception_isolated(runner: StrategyRunner) -> None:
    """策略 on_timer 抛异常不影响定时器循环和其他策略。"""
    good_count: list[int] = []

    class BadStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("bad-timer")

        async def on_timer(
            self, event: TimerEvent, ctx: StrategyContext
        ) -> None:
            raise RuntimeError("timer boom")

    class GoodStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return StrategyId("good-timer")

        async def on_timer(
            self, event: TimerEvent, ctx: StrategyContext
        ) -> None:
            good_count.append(1)

    runner.register(BadStrategy())
    runner.register(GoodStrategy())
    await runner.start()

    await asyncio.sleep(0.15)
    await runner.stop()

    assert len(good_count) >= 1


# --------------------------------------------------------------------------- StrategyContext


@pytest.mark.unit
async def test_context_submit_order_delegates() -> None:
    """StrategyContext.submit_order 构造 OrderRequest 并调用 order_manager。"""
    from finboard_shared.models import OrderRequest

    order_mgr = MagicMock()
    expected_order = _make_order()
    order_mgr.place_order = AsyncMock(return_value=expected_order)
    pos_mgr = MagicMock()
    pos_mgr.list_local = AsyncMock(return_value=[])
    acct_mgr = MagicMock()
    acct_mgr.current = AsyncMock(return_value=None)

    ctx = StrategyContext(
        strategy_id=StrategyId("ctx-test"),
        order_manager=order_mgr,
        position_manager=pos_mgr,
        account_manager=acct_mgr,
        account_id=ACCOUNT,
    )

    order = await ctx.submit_order(
        SYMBOL, Side.BUY, Decimal("100"), order_type=OrderType.LIMIT, price=Decimal("4.5")
    )

    assert order is expected_order
    order_mgr.place_order.assert_awaited_once()
    call_arg = order_mgr.place_order.call_args[0][0]
    assert isinstance(call_arg, OrderRequest)
    assert str(call_arg.strategy_id) == "ctx-test"
    assert call_arg.symbol == SYMBOL
    assert call_arg.side == Side.BUY
    assert call_arg.order_type == OrderType.LIMIT
    assert call_arg.price == Decimal("4.5")


@pytest.mark.unit
async def test_context_cancel_order_delegates() -> None:
    order_mgr = MagicMock()
    order_mgr.cancel_order = AsyncMock()
    pos_mgr = MagicMock()
    acct_mgr = MagicMock()

    ctx = StrategyContext(
        strategy_id=StrategyId("cancel-test"),
        order_manager=order_mgr,
        position_manager=pos_mgr,
        account_manager=acct_mgr,
        account_id=ACCOUNT,
    )

    await ctx.cancel_order("F-20250101-deadbeefcafebabe")
    order_mgr.cancel_order.assert_awaited_once_with("F-20250101-deadbeefcafebabe")


@pytest.mark.unit
async def test_context_list_positions_delegates() -> None:
    order_mgr = MagicMock()
    pos_mgr = MagicMock()
    pos_mgr.list_local = AsyncMock(return_value=[])
    acct_mgr = MagicMock()

    ctx = StrategyContext(
        strategy_id=StrategyId("pos-test"),
        order_manager=order_mgr,
        position_manager=pos_mgr,
        account_manager=acct_mgr,
        account_id=ACCOUNT,
    )

    result = await ctx.list_positions()
    pos_mgr.list_local.assert_awaited_once()
    assert result == []


# --------------------------------------------------------------------------- 全事件类型覆盖


@pytest.mark.unit
async def test_all_order_event_kinds(runner: StrategyRunner) -> None:
    """验证 created/submitted/filled/cancelled/rejected 五种事件类型都能路由。"""
    kinds: list[str] = []
    sid = StrategyId("all-kinds")

    class MyStrategy(Strategy):
        @property
        def strategy_id(self) -> StrategyId:
            return sid

        async def on_order_update(
            self, event: OrderEvent, ctx: StrategyContext
        ) -> None:
            kinds.append(event.kind)

    runner.register(MyStrategy())
    await runner.start()

    order = _make_order(strategy_id=sid)

    await runner._bus.publish(OrderCreated(order=order))
    await runner._bus.publish(OrderSubmitted(order=order))

    fill = Fill(
        fill_id="F-00000002",
        client_order_id=order.client_order_id,
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("100"),
        price=Decimal("4.50"),
        filled_at=datetime.now(UTC),
    )
    order.status = OrderStatus.FILLED
    await runner._bus.publish(OrderFilled(order=order, fill=fill))

    order2 = _make_order(strategy_id=sid, status=OrderStatus.CANCELLED)
    await runner._bus.publish(OrderCancelled(order=order2))

    order3 = _make_order(strategy_id=sid, status=OrderStatus.REJECTED)
    await runner._bus.publish(
        OrderRejected(order=order3, reason=RejectReason.RISK_CHECK_FAILED, message="blocked")
    )

    assert sorted(kinds) == ["cancelled", "created", "filled", "rejected", "submitted"]

    await runner.stop()
