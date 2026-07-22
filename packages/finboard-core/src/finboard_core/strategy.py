"""策略运行器 —— 事件驱动的策略执行框架。

链路::

    行情/定时 → on_market_data / on_timer
        → StrategyContext.submit_order
        → 风控 → OrderManager → Broker
        → 成交回报 → on_order_update

**红线**(AGENTS.md):

* 策略不得直接访问 ``BrokerAdapter`` / DB ``AsyncSession``;
* 策略不得直接修改持仓(只能通过成交事件驱动);
* 策略下单必须经 ``StrategyContext.submit_order`` → 风控 → ``OrderManager``。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from finboard_broker import MarketDataAdapter, MarketDataEvent
from finboard_core.account_manager import AccountManager
from finboard_core.bus import EventBus
from finboard_core.events import (
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from finboard_core.order_manager import OrderManager
from finboard_core.position_manager import PositionManager
from finboard_shared.identifiers import AccountId, StrategyId
from finboard_shared.models import Account, Fill, Order, OrderRequest, Position, Symbol
from finboard_shared.types import OrderType, Side

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- 策略可见事件


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """策略可见的订单事件(由内核 EventBus 事件转换而来)。

    只有 ``order.strategy_id`` 匹配的策略才会收到。
    """

    order: Order
    kind: str  # "created" | "submitted" | "filled" | "cancelled" | "rejected"
    fill: Fill | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """定时事件(由 ``StrategyRunner`` 周期触发)。"""

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------- Strategy ABC


class Strategy:
    """策略基类 —— 子类按需 override 回调方法。

    所有回调均为 ``async``;默认实现为空操作(no-op)。
    ``strategy_id`` 必须由子类提供,用于订单路由与审计。
    """

    @property
    def strategy_id(self) -> StrategyId:
        raise NotImplementedError

    async def on_start(self, ctx: StrategyContext) -> None:
        """策略启动时调用(在 ``StrategyRunner.start`` 中)。"""

    async def on_market_data(
        self, event: MarketDataEvent, ctx: StrategyContext
    ) -> None:
        """行情事件到达时调用(Tick / Bar)。"""

    async def on_order_update(
        self, event: OrderEvent, ctx: StrategyContext
    ) -> None:
        """该策略名下的订单状态变化时调用。"""

    async def on_timer(self, event: TimerEvent, ctx: StrategyContext) -> None:
        """定时回调(由 ``StrategyRunner`` 按 ``timer_interval`` 周期触发)。"""


# --------------------------------------------------------------------------- StrategyContext


class StrategyContext:
    """策略可用的安全 API。

    封装 ``OrderManager`` / ``PositionManager`` / ``AccountManager``,
    策略无法绕过风控直接访问 broker 或 DB。
    """

    def __init__(
        self,
        *,
        strategy_id: StrategyId,
        order_manager: OrderManager,
        position_manager: PositionManager,
        account_manager: AccountManager,
        account_id: AccountId,
    ) -> None:
        self._strategy_id = strategy_id
        self._order_manager = order_manager
        self._position_manager = position_manager
        self._account_manager = account_manager
        self._account_id = account_id

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    async def submit_order(
        self,
        symbol: Symbol,
        side: Side,
        quantity: Decimal,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> Order:
        """提交订单 → 风控 → OrderManager → Broker。"""
        request = OrderRequest(
            account_id=self._account_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            strategy_id=self._strategy_id,
            price=price,
        )
        return await self._order_manager.place_order(request)

    async def cancel_order(self, client_order_id: str) -> None:
        """撤销订单。"""
        await self._order_manager.cancel_order(client_order_id)

    async def list_positions(self) -> list[Position]:
        """查询本地持仓(只读)。"""
        return await self._position_manager.list_local()

    async def get_account(self) -> Account | None:
        """查询账户快照(只读)。"""
        return await self._account_manager.current()


# --------------------------------------------------------------------------- StrategyRunner


class StrategyRunner:
    """策略生命周期管理 + 事件路由 + 异常隔离。

    * 订阅 ``EventBus`` 订单事件 → 按 ``strategy_id`` 路由到对应策略;
    * 消费 ``MarketDataAdapter.events()`` → 推送到所有策略;
    * 周期触发 ``TimerEvent`` → 推送到所有策略。

    单个策略回调抛异常**不影响**其他策略和交易内核(异常被捕获并记录)。
    """

    def __init__(
        self,
        *,
        event_bus: EventBus,
        order_manager: OrderManager,
        position_manager: PositionManager,
        account_manager: AccountManager,
        account_id: AccountId,
        market_adapter: MarketDataAdapter | None = None,
        timer_interval: float = 60.0,
    ) -> None:
        self._bus = event_bus
        self._order_manager = order_manager
        self._position_manager = position_manager
        self._account_manager = account_manager
        self._account_id = account_id
        self._market_adapter = market_adapter
        self._timer_interval = timer_interval
        self._strategies: list[tuple[Strategy, StrategyContext]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def strategy_count(self) -> int:
        return len(self._strategies)

    def register(self, strategy: Strategy) -> None:
        """注册策略(在 ``start`` 之前调用)。"""
        ctx = StrategyContext(
            strategy_id=strategy.strategy_id,
            order_manager=self._order_manager,
            position_manager=self._position_manager,
            account_manager=self._account_manager,
            account_id=self._account_id,
        )
        self._strategies.append((strategy, ctx))

    async def start(self) -> None:
        """启动策略运行器(应在 ``kernel.start()`` 之后调用)。"""
        if self._running:
            return
        self._running = True

        # 订阅 EventBus 订单事件
        self._bus.subscribe(OrderCreated, self._on_order_created)
        self._bus.subscribe(OrderSubmitted, self._on_order_submitted)
        self._bus.subscribe(OrderFilled, self._on_order_filled)
        self._bus.subscribe(OrderCancelled, self._on_order_cancelled)
        self._bus.subscribe(OrderRejected, self._on_order_rejected)

        # 启动行情消费任务
        if self._market_adapter is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._consume_market_data(), name="strategy-market-data"
                )
            )

        # 启动定时器
        self._tasks.append(
            asyncio.create_task(self._timer_loop(), name="strategy-timer")
        )

        # 调用每个策略的 on_start
        for strategy, ctx in self._strategies:
            try:
                await strategy.on_start(ctx)
            except Exception:
                logger.exception(
                    "strategy.on_start_failed",
                    strategy_id=str(ctx.strategy_id),
                )

        logger.info(
            "strategy_runner.started",
            strategies=len(self._strategies),
            has_market_data=self._market_adapter is not None,
            timer_interval=self._timer_interval,
        )

    async def stop(self) -> None:
        """停止策略运行器(应在 ``kernel.stop()`` 之前调用)。"""
        if not self._running:
            return
        self._running = False

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        self._bus.unsubscribe(OrderCreated, self._on_order_created)
        self._bus.unsubscribe(OrderSubmitted, self._on_order_submitted)
        self._bus.unsubscribe(OrderFilled, self._on_order_filled)
        self._bus.unsubscribe(OrderCancelled, self._on_order_cancelled)
        self._bus.unsubscribe(OrderRejected, self._on_order_rejected)

        logger.info("strategy_runner.stopped")

    # ------------------------------------------------------------------ EventBus → 策略

    async def _on_order_created(self, event: OrderCreated) -> None:
        await self._dispatch_order(
            OrderEvent(order=event.order, kind="created", timestamp=event.timestamp)
        )

    async def _on_order_submitted(self, event: OrderSubmitted) -> None:
        await self._dispatch_order(
            OrderEvent(order=event.order, kind="submitted", timestamp=event.timestamp)
        )

    async def _on_order_filled(self, event: OrderFilled) -> None:
        await self._dispatch_order(
            OrderEvent(
                order=event.order,
                kind="filled",
                fill=event.fill,
                timestamp=event.timestamp,
            )
        )

    async def _on_order_cancelled(self, event: OrderCancelled) -> None:
        await self._dispatch_order(
            OrderEvent(
                order=event.order, kind="cancelled", timestamp=event.timestamp
            )
        )

    async def _on_order_rejected(self, event: OrderRejected) -> None:
        await self._dispatch_order(
            OrderEvent(
                order=event.order, kind="rejected", timestamp=event.timestamp
            )
        )

    async def _dispatch_order(self, event: OrderEvent) -> None:
        """按 ``strategy_id`` 路由订单事件到对应策略。"""
        sid = event.order.strategy_id
        if sid is None:
            return
        for strategy, ctx in self._strategies:
            if str(sid) != str(ctx.strategy_id):
                continue
            try:
                await strategy.on_order_update(event, ctx)
            except Exception:
                logger.exception(
                    "strategy.on_order_update_failed",
                    strategy_id=str(ctx.strategy_id),
                    event_kind=event.kind,
                )

    # ------------------------------------------------------------------ 行情 → 策略

    async def _consume_market_data(self) -> None:
        """消费行情适配器事件 → 推送到所有策略。"""
        assert self._market_adapter is not None
        try:
            async for md_event in self._market_adapter.events():
                if not self._running:
                    break
                for strategy, ctx in self._strategies:
                    try:
                        await strategy.on_market_data(md_event, ctx)
                    except Exception:
                        logger.exception(
                            "strategy.on_market_data_failed",
                            strategy_id=str(ctx.strategy_id),
                        )
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ 定时 → 策略

    async def _timer_loop(self) -> None:
        """周期触发 ``TimerEvent``。"""
        try:
            while self._running:
                await asyncio.sleep(self._timer_interval)
                if not self._running:
                    break
                timer_event = TimerEvent()
                for strategy, ctx in self._strategies:
                    try:
                        await strategy.on_timer(timer_event, ctx)
                    except Exception:
                        logger.exception(
                            "strategy.on_timer_failed",
                            strategy_id=str(ctx.strategy_id),
                        )
        except asyncio.CancelledError:
            pass
