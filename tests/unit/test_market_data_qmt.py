"""QMT 行情适配器单元测试。

通过注入 mock xtdata 到 ``sys.modules`` 来模拟 SDK 行为。
复用 ``test_broker_qmt.py`` 中的 mock 注入逻辑。
"""

from __future__ import annotations

import asyncio
import sys
import types
from decimal import Decimal
from typing import Any

import pytest


# --------------------------------------------------------------------------- mock xtdata 注入
class _FakeXtData:
    """模拟 xtdata 模块。"""

    def __init__(self) -> None:
        self._connected: bool = False
        self.subscriptions: list[tuple[str, str, Any]] = []  # (code, period, callback)
        self.unsubscriptions: list[tuple[str, str, Any]] = []
        self.download_calls: list[tuple[str, str, str, str]] = []
        self.get_data_result: Any = None

    def connect(self) -> None:
        self._connected = True

    def subscribe_quote(
        self,
        stock_code: str,
        period: str = "tick",
        count: int = -1,
        callback: Any = None,
    ) -> int:
        self.subscriptions.append((stock_code, period, callback))
        return 0

    def unsubscribe_quote(
        self,
        stock_code: str,
        period: str = "tick",
        callback: Any = None,
    ) -> int:
        self.unsubscriptions.append((stock_code, period, callback))
        return 0

    def download_history_data(
        self,
        stock_code: str,
        period: str,
        start_time: str,
        end_time: str,
    ) -> int:
        self.download_calls.append((stock_code, period, start_time, end_time))
        return 0

    def get_market_data_ex(
        self,
        field_list: list[str],
        stock_list: list[str],
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        **kwargs: Any,
    ) -> Any:
        return self.get_data_result


def _inject_mock_xtdata() -> _FakeXtData:
    """注入 fake xtdata 到 sys.modules;返回 mock 实例。"""
    if "xtquant.xtdata" in sys.modules:
        return sys.modules["xtquant.xtdata"]._mock  # type: ignore[attr-defined]

    # 确保 xtquant 包已存在(由 test_broker_qmt.py 注入,或这里注入)
    if "xtquant" not in sys.modules:
        pkg = types.ModuleType("xtquant")
        sys.modules["xtquant"] = pkg

    mock = _FakeXtData()
    mod = types.ModuleType("xtquant.xtdata")
    mod.connect = mock.connect  # type: ignore[attr-defined]
    mod.subscribe_quote = mock.subscribe_quote  # type: ignore[attr-defined]
    mod.unsubscribe_quote = mock.unsubscribe_quote  # type: ignore[attr-defined]
    mod.download_history_data = mock.download_history_data  # type: ignore[attr-defined]
    mod.get_market_data_ex = mock.get_market_data_ex  # type: ignore[attr-defined]
    mod._mock = mock  # type: ignore[attr-defined]

    sys.modules["xtquant"].xtdata = mod  # type: ignore[attr-defined]
    sys.modules["xtquant.xtdata"] = mod
    return mock


_mock_xtdata = _inject_mock_xtdata()

# 注入后 import 被测模块
from finboard_broker_qmt._market_mapping import (  # noqa: E402
    xtdata_bar_to_bar,
    xtdata_history_to_bars,
    xtdata_tick_to_tick,
)
from finboard_broker_qmt.market_adapter import (  # noqa: E402
    XTDATA_AVAILABLE,
    QmtMarketData,
    create_market_data,
)
from finboard_shared.models import Symbol  # noqa: E402
from finboard_shared.types import BarPeriod, Market  # noqa: E402


# --------------------------------------------------------------------------- mapping 测试
class TestMarketMapping:
    @pytest.mark.unit
    def test_xtdata_tick_to_tick(self) -> None:
        data = {
            "last_price": 3.85,
            "open": 3.80,
            "high": 3.90,
            "low": 3.75,
            "volume": 100000,
            "amount": 385000.0,
            "last_volume": 100,
            "bid_price": [3.84, 3.83],
            "bid_volume": [500, 300],
            "ask_price": [3.86, 3.87],
            "ask_volume": [200, 400],
            "time": 1700000000000,  # ms
        }
        tick = xtdata_tick_to_tick("510300.SH", data)
        assert tick.symbol.code == "510300.SH"
        assert tick.last_price == Decimal("3.85")
        assert tick.high == Decimal("3.90")
        assert tick.bid_price == Decimal("3.84")
        assert tick.bid_volume == Decimal("500")
        assert tick.ask_price == Decimal("3.86")
        assert tick.timestamp.tzinfo is not None

    @pytest.mark.unit
    def test_xtdata_tick_no_bid_ask(self) -> None:
        data = {"last_price": 10.5}
        tick = xtdata_tick_to_tick("600000.SH", data)
        assert tick.last_price == Decimal("10.5")
        assert tick.bid_price == Decimal("0")
        assert tick.ask_price == Decimal("0")

    @pytest.mark.unit
    def test_xtdata_bar_to_bar(self) -> None:
        data = {
            "time": 1700000000000,
            "open": 3.80,
            "high": 3.90,
            "low": 3.75,
            "close": 3.85,
            "volume": 50000,
            "amount": 192500.0,
        }
        bar = xtdata_bar_to_bar("510300.SH", "1m", data)
        assert bar.symbol.code == "510300.SH"
        assert bar.period is BarPeriod.M1
        assert bar.open == Decimal("3.80")
        assert bar.close == Decimal("3.85")
        assert bar.volume == Decimal("50000")

    @pytest.mark.unit
    def test_xtdata_history_to_bars_dict_of_lists(self) -> None:
        """get_market_data_ex 返回 {code: [[time,o,h,l,c,v,a], ...]} 格式。"""
        raw = {
            "510300.SH": [
                [1700000000000, 3.80, 3.90, 3.75, 3.85, 50000, 192500.0],
                [1700000060000, 3.85, 3.95, 3.80, 3.92, 60000, 234000.0],
            ]
        }
        bars = xtdata_history_to_bars("510300.SH", "1m", raw)
        assert len(bars) == 2
        assert bars[0].open == Decimal("3.80")
        assert bars[1].close == Decimal("3.92")
        assert bars[0].period is BarPeriod.M1

    @pytest.mark.unit
    def test_xtdata_history_to_bars_empty(self) -> None:
        assert xtdata_history_to_bars("510300.SH", "1m", None) == []
        assert xtdata_history_to_bars("510300.SH", "1m", {}) == []


# --------------------------------------------------------------------------- adapter 测试
class TestQmtMarketData:
    def setup_method(self) -> None:
        _mock_xtdata.subscriptions.clear()
        _mock_xtdata.unsubscriptions.clear()
        _mock_xtdata.download_calls.clear()
        _mock_xtdata.get_data_result = None

    @pytest.mark.unit
    def test_xtdata_available(self) -> None:
        assert XTDATA_AVAILABLE is True

    @pytest.mark.unit
    def test_create_market_data(self) -> None:
        md = create_market_data(path="/fake")
        assert isinstance(md, QmtMarketData)
        assert md.kind == "qmt"

    @pytest.mark.unit
    async def test_connect_disconnect(self) -> None:
        md = QmtMarketData()
        await md.connect()
        assert await md.is_connected()
        await md.disconnect()
        assert not await md.is_connected()

    @pytest.mark.unit
    async def test_subscribe_tick(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_tick([sym])

        assert len(_mock_xtdata.subscriptions) == 1
        assert _mock_xtdata.subscriptions[0][0] == "510300.SH"
        assert _mock_xtdata.subscriptions[0][1] == "tick"
        await md.disconnect()

    @pytest.mark.unit
    async def test_unsubscribe_tick(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_tick([sym])
        await md.unsubscribe_tick([sym])

        assert len(_mock_xtdata.unsubscriptions) == 1
        assert _mock_xtdata.unsubscriptions[0][0] == "510300.SH"
        await md.disconnect()

    @pytest.mark.unit
    async def test_subscribe_bar(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_bar([sym], BarPeriod.M5)

        assert len(_mock_xtdata.subscriptions) == 1
        assert _mock_xtdata.subscriptions[0][0] == "510300.SH"
        assert _mock_xtdata.subscriptions[0][1] == "5m"
        await md.disconnect()

    @pytest.mark.unit
    async def test_subscribe_idempotent(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_tick([sym])
        await md.subscribe_tick([sym])  # 重复

        assert len(_mock_xtdata.subscriptions) == 1
        await md.disconnect()

    @pytest.mark.unit
    async def test_disconnect_unsubscribes_all(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym1 = Symbol(code="510300.SH", market=Market.A_SHARE)
        sym2 = Symbol(code="600000.SH", market=Market.A_SHARE)
        await md.subscribe_tick([sym1, sym2])
        await md.subscribe_bar([sym1], BarPeriod.M1)

        _mock_xtdata.unsubscriptions.clear()
        await md.disconnect()

        # disconnect 应退订全部(2 tick + 1 bar = 3)
        assert len(_mock_xtdata.unsubscriptions) == 3

    @pytest.mark.unit
    async def test_not_connected_raises(self) -> None:
        md = QmtMarketData()
        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        with pytest.raises(RuntimeError, match="未连接"):
            await md.subscribe_tick([sym])

    @pytest.mark.unit
    async def test_tick_callback_bridges_to_event_queue(self) -> None:
        md = QmtMarketData()
        await md.connect()
        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_tick([sym])

        # 排空 CONNECTED 事件
        await md._event_queue.get()

        # 获取注册的回调函数,在测试线程模拟 xtdata 回调
        callback = md._tick_callbacks["510300.SH"]
        tick_data = {
            "last_price": 3.92,
            "open": 3.80,
            "high": 3.95,
            "low": 3.78,
            "volume": 10000,
            "time": 1700000000000,
            "bid_price": [3.91],
            "bid_volume": [100],
            "ask_price": [3.93],
            "ask_volume": [200],
        }

        # 回调在另一线程被调用,但 call_soon_threadsafe 会转到当前 loop
        callback(tick_data)

        # 给 event loop 时间处理 call_soon_threadsafe
        await asyncio.sleep(0.05)

        event = md._event_queue.get_nowait()
        assert event.type is not None  # TICK
        from finboard_broker.market_base import MarketDataEventType

        assert event.type is MarketDataEventType.TICK
        assert event.tick is not None
        assert event.tick.last_price == Decimal("3.92")
        assert event.tick.symbol.code == "510300.SH"
        await md.disconnect()

    @pytest.mark.unit
    async def test_bar_callback_bridges_to_event_queue(self) -> None:
        from finboard_broker.market_base import MarketDataEventType

        md = QmtMarketData()
        await md.connect()
        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        await md.subscribe_bar([sym], BarPeriod.M1)

        await md._event_queue.get()  # CONNECTED

        callback = md._bar_callbacks[("510300.SH", "1m")]
        bar_data = {
            "time": 1700000000000,
            "open": 3.80,
            "high": 3.90,
            "low": 3.75,
            "close": 3.85,
            "volume": 50000,
            "amount": 192500.0,
        }

        callback(bar_data)
        await asyncio.sleep(0.05)

        event = md._event_queue.get_nowait()
        assert event.type is MarketDataEventType.BAR
        assert event.bar is not None
        assert event.bar.close == Decimal("3.85")
        await md.disconnect()

    @pytest.mark.unit
    async def test_download_history_bar(self) -> None:
        md = QmtMarketData()
        await md.connect()

        sym = Symbol(code="510300.SH", market=Market.A_SHARE)
        _mock_xtdata.get_data_result = {
            "510300.SH": [
                [1700000000000, 3.80, 3.90, 3.75, 3.85, 50000, 192500.0],
                [1700000060000, 3.85, 3.95, 3.80, 3.92, 60000, 234000.0],
            ]
        }

        bars = await md.download_history_bar(
            sym, BarPeriod.D1, "20240101", "20240131"
        )

        assert len(bars) == 2
        assert bars[0].open == Decimal("3.80")
        assert bars[1].close == Decimal("3.92")
        assert bars[0].period is BarPeriod.D1

        # download_history_data 应被调用
        assert len(_mock_xtdata.download_calls) == 1
        assert _mock_xtdata.download_calls[0][0] == "510300.SH"
        await md.disconnect()
