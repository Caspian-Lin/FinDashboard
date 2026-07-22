"""``MarketDataAdapter`` 抽象基类与行情事件类型。

与 :class:`BrokerAdapter` 平行的行情接口抽象。行情为**只读**数据,
不触及交易安全红线,但实现方仍需:

* 所有方法 ``async``;底层同步 SDK 用 ``run_in_executor`` 桥接;
* 回调线程 → asyncio 事件桥接(复用 ``call_soon_threadsafe`` 模式);
* 事件流保证 at-least-once(断线重连后重放当日缓存)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from finboard_shared.models import Bar, Symbol, Tick
from finboard_shared.types import BarPeriod


class MarketDataEventType(StrEnum):
    CONNECTED = "md_connected"
    DISCONNECTED = "md_disconnected"
    TICK = "tick"
    BAR = "bar"
    ERROR = "md_error"


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    """行情推送的原子事件。

    ``tick`` / ``bar`` 二选一,由 ``type`` 区分。
    """

    type: MarketDataEventType
    tick: Tick | None = None
    bar: Bar | None = None
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class MarketDataAdapter(ABC):
    """行情接口统一抽象。"""

    @property
    @abstractmethod
    def kind(self) -> str:
        """实现的行情源标识(如 ``"mock"`` / ``"qmt"``)。"""

    @abstractmethod
    async def connect(self) -> None:
        """建立行情连接。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """优雅断开。"""

    @abstractmethod
    async def is_connected(self) -> bool:
        """连接健康度。"""

    # ----------------------------- 订阅 -----------------------------

    @abstractmethod
    async def subscribe_tick(self, symbols: list[Symbol]) -> None:
        """订阅实时 Tick 推送;重复订阅同一 symbol 幂等。"""

    @abstractmethod
    async def unsubscribe_tick(self, symbols: list[Symbol]) -> None:
        """退订 Tick。"""

    @abstractmethod
    async def subscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        """订阅 K 线推送。"""

    @abstractmethod
    async def unsubscribe_bar(
        self, symbols: list[Symbol], period: BarPeriod
    ) -> None:
        """退订 K 线。"""

    # ----------------------------- 历史数据 -----------------------------

    @abstractmethod
    async def download_history_bar(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start_time: str,
        end_time: str,
    ) -> list[Bar]:
        """下载历史 K 线。

        ``start_time`` / ``end_time`` 格式为 ``YYYYMMDD`` 或
        ``YYYYMMDDHHMMSS``(由实现方决定)。返回按时间升序的 Bar 列表。
        """

    # ----------------------------- 事件流 -----------------------------

    @abstractmethod
    def events(self) -> AsyncIterator[MarketDataEvent]:
        """订阅行情事件流;返回 async generator,调用方 ``async for`` 消费。"""
