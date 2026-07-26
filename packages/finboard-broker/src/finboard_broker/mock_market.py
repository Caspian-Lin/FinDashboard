"""``MockMarketData`` —— 用于 CI / 单元测试的行情适配器。

不依赖任何外部 SDK,所有推送由测试代码主动注入(:meth:`inject_tick` /
:meth:`inject_bar`)。支持按 symbol 订阅过滤。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from finboard_broker.market_base import MarketDataAdapter, MarketDataEvent, MarketDataEventType
from finboard_shared.models import Bar, Symbol, Tick
from finboard_shared.types import BarPeriod


class MockMarketData(MarketDataAdapter):
    """内存行情适配器,用于测试。

    用法::

        md = MockMarketData()
        await md.connect()
        await md.subscribe_tick([symbol])
        await md.inject_tick(tick)        # 模拟推送
        event = await md.next_event()     # 消费
    """

    def __init__(self) -> None:
        self._connected: bool = False
        self._event_queue: asyncio.Queue[MarketDataEvent] = asyncio.Queue()
        self._tick_subs: set[str] = set()
        self._bar_subs: set[tuple[str, str]] = set()  # (code, period)

    @property
    def kind(self) -> str:
        return "mock"

    async def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        await self._event_queue.put(
            MarketDataEvent(type=MarketDataEventType.CONNECTED)
        )

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        await self._event_queue.put(
            MarketDataEvent(type=MarketDataEventType.DISCONNECTED)
        )

    async def is_connected(self) -> bool:
        return self._connected

    async def subscribe_tick(self, symbols: list[Symbol]) -> None:
        for s in symbols:
            self._tick_subs.add(s.code)

    async def unsubscribe_tick(self, symbols: list[Symbol]) -> None:
        for s in symbols:
            self._tick_subs.discard(s.code)

    async def subscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        for s in symbols:
            self._bar_subs.add((s.code, period.value))

    async def unsubscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        for s in symbols:
            self._bar_subs.discard((s.code, period.value))

    async def download_history_bar(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start_time: str,
        end_time: str,
    ) -> list[Bar]:
        return []

    def events(self) -> AsyncIterator[MarketDataEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[MarketDataEvent]:
        while self._connected or not self._event_queue.empty():
            event = await self._event_queue.get()
            yield event

    # ------------------------------------------------------------------ 测试注入
    async def inject_tick(self, tick: Tick) -> None:
        """模拟行情源推送一个 Tick;仅当 symbol 已订阅时才会推入事件队列。"""
        if tick.symbol.code in self._tick_subs:
            await self._event_queue.put(
                MarketDataEvent(type=MarketDataEventType.TICK, tick=tick)
            )

    async def inject_bar(self, bar: Bar) -> None:
        """模拟行情源推送一根 K 线。"""
        key = (bar.symbol.code, bar.period.value)
        if key in self._bar_subs:
            await self._event_queue.put(
                MarketDataEvent(type=MarketDataEventType.BAR, bar=bar)
            )

    async def inject_error(self, message: str) -> None:
        """模拟行情源错误。"""
        await self._event_queue.put(
            MarketDataEvent(
                type=MarketDataEventType.ERROR, message=message
            )
        )

    @property
    def tick_subscriptions(self) -> set[str]:
        """已订阅的 symbol code 集合(测试检查用)。"""
        return set(self._tick_subs)

    @property
    def bar_subscriptions(self) -> set[tuple[str, str]]:
        """已订阅的 (code, period) 集合(测试检查用)。"""
        return set(self._bar_subs)
