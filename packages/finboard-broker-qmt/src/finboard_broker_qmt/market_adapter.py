"""QMT (xtdata) MarketDataAdapter 实现 —— A 股实时行情。

架构要点与 :mod:`finboard_broker_qmt.adapter` (QmtBroker) 一致:

1. **xtdata 是同步 SDK**,所有调用(``subscribe_quote`` 等)通过
   ``loop.run_in_executor`` 转到线程池。
2. **回调在 xtdata 内部线程触发**,通过 ``call_soon_threadsafe`` 桥接到
   asyncio 事件循环。
3. **xtdata 仅 Windows + miniQMT 可用**;``try: import xtquant`` 失败时
   ``XTDATA_AVAILABLE`` 为 False,工厂函数抛清晰错误。
4. 行情为**只读**数据,不触及交易安全红线。

xtdata 主要 API::

    xtdata.subscribe_quote(stock_code, period, count, callback)
    xtdata.unsubscribe_quote(stock_code, period, callback)
    xtdata.download_history_data(stock_code, period, start_time, end_time)
    xtdata.get_market_data_ex(field_list, stock_list, period, start_time, end_time)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from finboard_broker.market_base import (
    MarketDataAdapter,
    MarketDataEvent,
    MarketDataEventType,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- xtdata 可选导入
try:
    from xtquant import xtdata

    XTDATA_AVAILABLE = True
except ImportError:
    xtdata = None
    XTDATA_AVAILABLE = False

XTDATA_INSTALL_HINT = (
    "xtdata 未安装。xtdata 是 xtquant 的行情模块,"
    " 仅在安装了 miniQMT 的 Windows 环境可用。\n"
    " 请确认:\n"
    "  1. 已安装 QMT 客户端并启动 miniQMT;\n"
    "  2. xtquant 路径已加入 PYTHONPATH;\n"
    "  3. 运行环境为 Windows。\n"
    " 本地开发 / CI 请使用 MockMarketData"
)


class QmtMarketData(MarketDataAdapter):
    """QMT (xtdata) 行情适配器。

    构造参数:

    * ``path`` —— miniQMT 的 ``userdata_mini`` 路径(可选,部分版本不需要)。

    生命周期:``connect`` → ``subscribe_*`` → ``events()`` 消费 → ``disconnect``。
    """

    def __init__(self, *, path: str = "") -> None:
        if not XTDATA_AVAILABLE:
            raise ImportError(XTDATA_INSTALL_HINT)

        self._path = path
        self._connected: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_queue: asyncio.Queue[MarketDataEvent] = asyncio.Queue()
        self._tick_callbacks: dict[str, Any] = {}  # code → callback fn
        self._bar_callbacks: dict[tuple[str, str], Any] = {}  # (code, period) → fn
        self._tick_subs: set[str] = set()
        self._bar_subs: set[tuple[str, str]] = set()

    @property
    def kind(self) -> str:
        return "qmt"

    # ------------------------------------------------------------------ 连接
    async def connect(self) -> None:
        if self._connected:
            return
        self._loop = asyncio.get_running_loop()
        # xtdata 不需要显式 connect,但部分版本需要 xtdata.connect() 来初始化
        if hasattr(xtdata, "connect"):
            await self._loop.run_in_executor(None, xtdata.connect)
        self._connected = True
        await self._event_queue.put(
            MarketDataEvent(type=MarketDataEventType.CONNECTED)
        )
        logger.info("qmt_market.connected")

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False

        assert self._loop is not None
        assert xtdata is not None

        # 退订所有 tick 订阅
        for code in list(self._tick_subs):
            cb = self._tick_callbacks.pop(code, None)
            if cb is not None:
                with contextlib.suppress(Exception):
                    await self._loop.run_in_executor(
                        None, self._sync_unsubscribe_quote, code, "tick", cb
                    )

        # 退订所有 bar 订阅
        for (code, period_str), cb in list(self._bar_callbacks.items()):
            with contextlib.suppress(Exception):
                await self._loop.run_in_executor(
                    None, self._sync_unsubscribe_quote, code, period_str, cb
                )

        self._tick_subs.clear()
        self._bar_subs.clear()
        self._bar_callbacks.clear()

        await self._event_queue.put(
            MarketDataEvent(type=MarketDataEventType.DISCONNECTED)
        )
        logger.info("qmt_market.disconnected")

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 订阅
    async def subscribe_tick(self, symbols: list[Symbol]) -> None:
        self._require_connected()
        assert self._loop is not None
        assert xtdata is not None

        for sym in symbols:
            code = sym.code
            if code in self._tick_subs:
                continue

            cb = self._make_tick_callback(code)
            self._tick_callbacks[code] = cb
            self._tick_subs.add(code)

            await self._loop.run_in_executor(
                None, self._sync_subscribe_tick, code, cb
            )
            logger.info("qmt_market.subscribed_tick", extra={"symbol": code})

    async def unsubscribe_tick(self, symbols: list[Symbol]) -> None:
        self._require_connected()
        assert self._loop is not None
        assert xtdata is not None

        for sym in symbols:
            code = sym.code
            cb = self._tick_callbacks.pop(code, None)
            self._tick_subs.discard(code)
            if cb is not None:
                await self._loop.run_in_executor(
                    None, self._sync_unsubscribe_quote, code, "tick", cb
                )
                logger.info("qmt_market.unsubscribed_tick", extra={"symbol": code})

    async def subscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        self._require_connected()
        assert self._loop is not None
        assert xtdata is not None

        period_str = period.value
        for sym in symbols:
            code = sym.code
            key = (code, period_str)
            if key in self._bar_subs:
                continue

            cb = self._make_bar_callback(code, period_str)
            self._bar_callbacks[key] = cb
            self._bar_subs.add(key)

            await self._loop.run_in_executor(
                None, self._sync_subscribe_bar, code, period_str, cb
            )
            logger.info(
                "qmt_market.subscribed_bar",
                extra={"symbol": code, "period": period_str},
            )

    async def unsubscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        self._require_connected()
        assert self._loop is not None
        assert xtdata is not None

        period_str = period.value
        for sym in symbols:
            code = sym.code
            key = (code, period_str)
            cb = self._bar_callbacks.pop(key, None)
            self._bar_subs.discard(key)
            if cb is not None:
                await self._loop.run_in_executor(
                    None, self._sync_unsubscribe_quote, code, period_str, cb
                )
                logger.info(
                    "qmt_market.unsubscribed_bar",
                    extra={"symbol": code, "period": period_str},
                )

    # ------------------------------------------------------------------ 历史数据
    async def download_history_bar(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start_time: str,
        end_time: str,
    ) -> list[Bar]:
        self._require_connected()
        assert self._loop is not None
        assert xtdata is not None

        code = symbol.code
        period_str = period.value

        # 先下载到本地缓存
        await self._loop.run_in_executor(
            None,
            self._sync_download_history_data,
            code, period_str, start_time, end_time,
        )

        # 再从缓存读取
        raw = await self._loop.run_in_executor(
            None,
            self._sync_get_market_data_ex,
            code, period_str, start_time, end_time,
        )

        from finboard_broker_qmt._market_mapping import xtdata_history_to_bars

        return xtdata_history_to_bars(code, period_str, raw)

    # ------------------------------------------------------------------ 事件流
    def events(self) -> AsyncIterator[MarketDataEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[MarketDataEvent]:
        while self._connected or not self._event_queue.empty():
            event = await self._event_queue.get()
            yield event

    # ------------------------------------------------------------------ 同步 SDK 包装(executor 线程内调用)
    @staticmethod
    def _sync_subscribe_tick(code: str, callback: Any) -> None:
        assert xtdata is not None
        xtdata.subscribe_quote(code, period="tick", count=-1, callback=callback)

    @staticmethod
    def _sync_subscribe_bar(code: str, period: str, callback: Any) -> None:
        assert xtdata is not None
        xtdata.subscribe_quote(code, period=period, count=-1, callback=callback)

    @staticmethod
    def _sync_unsubscribe_quote(code: str, period: str, callback: Any) -> None:
        assert xtdata is not None
        xtdata.unsubscribe_quote(code, period, callback)

    @staticmethod
    def _sync_download_history_data(
        code: str, period: str, start_time: str, end_time: str
    ) -> None:
        assert xtdata is not None
        xtdata.download_history_data(code, period, start_time, end_time)

    @staticmethod
    def _sync_get_market_data_ex(
        code: str, period: str, start_time: str, end_time: str
    ) -> Any:
        assert xtdata is not None
        return xtdata.get_market_data_ex(
            [], [code], period=period,
            start_time=start_time, end_time=end_time,
        )

    # ------------------------------------------------------------------ 回调桥接
    def _make_tick_callback(self, code: str) -> Any:
        """创建 tick 回调闭包;在 xtdata 内部线程被调用。"""
        loop = self._loop
        assert loop is not None

        def _on_tick(data: dict[str, Any]) -> None:
            try:
                from finboard_broker_qmt._market_mapping import xtdata_tick_to_tick

                tick = xtdata_tick_to_tick(code, data)
                loop.call_soon_threadsafe(
                    self._enqueue_event,
                    MarketDataEvent(type=MarketDataEventType.TICK, tick=tick),
                )
            except Exception:
                logger.exception("qmt_market.tick_callback_error")

        return _on_tick

    def _make_bar_callback(self, code: str, period: str) -> Any:
        """创建 bar 回调闭包;在 xtdata 内部线程被调用。"""
        loop = self._loop
        assert loop is not None

        def _on_bar(data: dict[str, Any]) -> None:
            try:
                from finboard_broker_qmt._market_mapping import xtdata_bar_to_bar

                bar = xtdata_bar_to_bar(code, period, data)
                loop.call_soon_threadsafe(
                    self._enqueue_event,
                    MarketDataEvent(type=MarketDataEventType.BAR, bar=bar),
                )
            except Exception:
                logger.exception("qmt_market.bar_callback_error")

        return _on_bar

    def _enqueue_event(self, event: MarketDataEvent) -> None:
        """在 asyncio 线程中安全地入队事件。"""
        self._event_queue.put_nowait(event)

    # ------------------------------------------------------------------ 内部
    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("QmtMarketData 未连接")


# --------------------------------------------------------------------------- 工厂
def create_market_data(**kwargs: Any) -> MarketDataAdapter:
    """供工厂调用;``kwargs`` 透传 path 等参数。"""
    if not XTDATA_AVAILABLE:
        raise ImportError(XTDATA_INSTALL_HINT)
    path = kwargs.get("path", "")
    return QmtMarketData(path=path)
