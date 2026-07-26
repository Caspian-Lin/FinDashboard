"""MockMarketData 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finboard_broker.market_base import MarketDataEventType
from finboard_broker.mock_market import MockMarketData
from finboard_shared.models import Bar, Symbol, Tick
from finboard_shared.types import BarPeriod, Market


def _mk_symbol(code: str = "510300.SH") -> Symbol:
    return Symbol(code=code, market=Market.A_SHARE)


def _mk_tick(code: str = "510300.SH", last_price: str = "3.85") -> Tick:
    return Tick(
        symbol=_mk_symbol(code),
        last_price=Decimal(last_price),
        volume=Decimal("10000"),
    )


def _mk_bar(code: str = "510300.SH", period: BarPeriod = BarPeriod.M1) -> Bar:
    return Bar(
        symbol=_mk_symbol(code),
        period=period,
        timestamp=datetime.now(UTC),
        open=Decimal("3.80"),
        high=Decimal("3.90"),
        low=Decimal("3.75"),
        close=Decimal("3.85"),
        volume=Decimal("50000"),
    )


class TestMockMarketData:
    @pytest.mark.unit
    async def test_connect_disconnect(self) -> None:
        md = MockMarketData()
        assert not await md.is_connected()

        await md.connect()
        assert await md.is_connected()

        # CONNECTED 事件
        event = await md._event_queue.get()
        assert event.type is MarketDataEventType.CONNECTED

        await md.disconnect()
        assert not await md.is_connected()

        event = await md._event_queue.get()
        assert event.type is MarketDataEventType.DISCONNECTED

    @pytest.mark.unit
    async def test_subscribe_unsubscribe_tick(self) -> None:
        md = MockMarketData()
        await md.connect()

        sym1 = _mk_symbol("510300.SH")
        sym2 = _mk_symbol("600000.SH")

        await md.subscribe_tick([sym1, sym2])
        assert md.tick_subscriptions == {"510300.SH", "600000.SH"}

        # 重复订阅幂等
        await md.subscribe_tick([sym1])
        assert md.tick_subscriptions == {"510300.SH", "600000.SH"}

        await md.unsubscribe_tick([sym1])
        assert md.tick_subscriptions == {"600000.SH"}

        await md.disconnect()

    @pytest.mark.unit
    async def test_inject_tick_delivers_event(self) -> None:
        md = MockMarketData()
        await md.connect()
        await md.subscribe_tick([_mk_symbol("510300.SH")])

        # 排空 CONNECTED 事件
        await md._event_queue.get()

        tick = _mk_tick("510300.SH", "3.92")
        await md.inject_tick(tick)

        event = await md._event_queue.get()
        assert event.type is MarketDataEventType.TICK
        assert event.tick is not None
        assert event.tick.last_price == Decimal("3.92")
        await md.disconnect()

    @pytest.mark.unit
    async def test_inject_tick_filtered_by_subscription(self) -> None:
        md = MockMarketData()
        await md.connect()
        await md.subscribe_tick([_mk_symbol("510300.SH")])

        # 排空
        await md._event_queue.get()

        # 未订阅的 symbol 不应推入事件
        tick = _mk_tick("600000.SH", "10.50")
        await md.inject_tick(tick)
        assert md._event_queue.empty()
        await md.disconnect()

    @pytest.mark.unit
    async def test_subscribe_unsubscribe_bar(self) -> None:
        md = MockMarketData()
        await md.connect()

        sym = _mk_symbol("510300.SH")
        await md.subscribe_bar([sym], BarPeriod.M1)
        assert md.bar_subscriptions == {("510300.SH", "1m")}

        await md.subscribe_bar([sym], BarPeriod.M5)
        assert md.bar_subscriptions == {
            ("510300.SH", "1m"),
            ("510300.SH", "5m"),
        }

        await md.unsubscribe_bar([sym], BarPeriod.M1)
        assert md.bar_subscriptions == {("510300.SH", "5m")}
        await md.disconnect()

    @pytest.mark.unit
    async def test_inject_bar_delivers_event(self) -> None:
        md = MockMarketData()
        await md.connect()
        sym = _mk_symbol("510300.SH")
        await md.subscribe_bar([sym], BarPeriod.M5)

        # 排空 CONNECTED
        await md._event_queue.get()

        bar = _mk_bar("510300.SH", BarPeriod.M5)
        await md.inject_bar(bar)

        event = await md._event_queue.get()
        assert event.type is MarketDataEventType.BAR
        assert event.bar is not None
        assert event.bar.close == Decimal("3.85")
        await md.disconnect()

    @pytest.mark.unit
    async def test_inject_bar_filtered_by_period(self) -> None:
        md = MockMarketData()
        await md.connect()
        sym = _mk_symbol("510300.SH")
        await md.subscribe_bar([sym], BarPeriod.M1)

        await md._event_queue.get()

        # 不同周期的 bar 不应推入
        bar = _mk_bar("510300.SH", BarPeriod.M5)
        await md.inject_bar(bar)
        assert md._event_queue.empty()
        await md.disconnect()

    @pytest.mark.unit
    async def test_inject_error(self) -> None:
        md = MockMarketData()
        await md.connect()
        await md._event_queue.get()

        await md.inject_error("行情断线")

        event = await md._event_queue.get()
        assert event.type is MarketDataEventType.ERROR
        assert "行情断线" in event.message
        await md.disconnect()

    @pytest.mark.unit
    async def test_events_async_iterator(self) -> None:
        md = MockMarketData()
        await md.connect()
        sym = _mk_symbol("510300.SH")
        await md.subscribe_tick([sym])

        await md.inject_tick(_mk_tick("510300.SH", "4.00"))

        events = []
        async for event in md.events():
            events.append(event)
            if len(events) >= 2:  # CONNECTED + TICK
                break

        assert any(e.type is MarketDataEventType.TICK for e in events)
        await md.disconnect()

    @pytest.mark.unit
    async def test_download_history_bar_empty(self) -> None:
        md = MockMarketData()
        await md.connect()
        bars = await md.download_history_bar(
            _mk_symbol("510300.SH"), BarPeriod.D1, "20240101", "20240131"
        )
        assert bars == []
        await md.disconnect()
