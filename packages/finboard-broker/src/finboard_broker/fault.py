"""``FaultInjectionBroker`` —— 可注入故障的 BrokerAdapter 装饰器。

用于 §9.1 故障注入测试。包装任意 :class:`BrokerAdapter`(通常是 ``MockBroker``),
通过编程式 API 在特定调用上注入:

* **超时** —— ``place_order`` / ``cancel_order`` / ``query_*`` 抛 ``BrokerTimeoutError``
* **拒单** —— ``place_order`` 返回 ``SubmissionResult(accepted=False)``
* **延迟** —— 所有方法注入可配置延迟(模拟网络抖动)
* **断连** —— 向事件流注入 ``DISCONNECTED`` 事件
* **重复回报** —— 重放指定的 ``BrokerEvent``
* **查询异常** —— ``query_order`` 抛非超时异常(模拟券商返回异常数据)

所有注入器都支持 ``times`` 参数控制故障触发次数,到期后自动恢复正常委托。

设计原则:
* 装饰器模式 —— 不修改被包装的 broker,测试结束后可恢复;
* 线程安全不做考虑(asyncio 单线程模型);
* 计数器递减式 —— ``inject_*`` 增加待触发次数,每次匹配后递减。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.exceptions import BrokerTimeoutError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Order, Position
from finboard_shared.types import BrokerKind, RejectReason


class FaultInjectionBroker(BrokerAdapter):
    """装饰任意 :class:`BrokerAdapter`,按需注入故障。"""

    def __init__(self, inner: BrokerAdapter) -> None:
        self._inner = inner

        # --- 剩余待触发次数(递减式计数器) ---
        self._place_timeouts: int = 0
        self._cancel_timeouts: int = 0
        self._query_timeouts: int = 0
        self._place_rejects: int = 0
        self._place_reject_reason: RejectReason = RejectReason.BROKER_REJECTED
        self._query_errors: int = 0

        # --- 延迟配置(秒,0 = 不延迟) ---
        self.place_delay: float = 0.0
        self.cancel_delay: float = 0.0
        self.query_delay: float = 0.0

        # --- 事件注入 ---
        self._pending_events: list[BrokerEvent] = []

    # ------------------------------------------------------------------ 元数据
    @property
    def kind(self) -> BrokerKind:
        return self._inner.kind

    @property
    def inner(self) -> BrokerAdapter:
        """直接访问被包装的 broker(测试辅助,如调用 MockBroker.match_limit_order)。"""
        return self._inner

    # ------------------------------------------------------------------ 注入 API
    def inject_place_timeout(self, times: int = 1) -> None:
        """接下来 ``times`` 次 ``place_order`` 抛 ``BrokerTimeoutError``。"""
        self._place_timeouts += times

    def inject_cancel_timeout(self, times: int = 1) -> None:
        """接下来 ``times`` 次 ``cancel_order`` 抛 ``BrokerTimeoutError``。"""
        self._cancel_timeouts += times

    def inject_query_timeout(self, times: int = 1) -> None:
        """接下来 ``times`` 次查询类方法抛 ``BrokerTimeoutError``。"""
        self._query_timeouts += times

    def inject_reject(
        self, times: int = 1, reason: RejectReason = RejectReason.BROKER_REJECTED
    ) -> None:
        """接下来 ``times`` 次 ``place_order`` 返回拒单结果。"""
        self._place_rejects += times
        self._place_reject_reason = reason

    def inject_query_error(self, times: int = 1) -> None:
        """接下来 ``times`` 次 ``query_order`` 抛非超时异常(模拟券商异常数据)。"""
        self._query_errors += times

    def inject_disconnect(self) -> None:
        """向事件流注入一个 ``DISCONNECTED`` 事件。"""
        self._pending_events.append(
            BrokerEvent(type=BrokerEventType.DISCONNECTED)
        )

    def inject_event(self, event: BrokerEvent) -> None:
        """向事件流注入任意事件(用于重复回报 / 乱序回报测试)。"""
        self._pending_events.append(event)

    # ------------------------------------------------------------------ 连接
    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        await self._inner.connect(account_id, credentials)

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def is_connected(self) -> bool:
        return await self._inner.is_connected()

    # ------------------------------------------------------------------ 查询
    async def query_account(self) -> Account:
        if self._query_timeouts > 0:
            self._query_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: query_account 超时")
        if self.query_delay > 0:
            await asyncio.sleep(self.query_delay)
        return await self._inner.query_account()

    async def query_positions(self) -> list[Position]:
        if self._query_timeouts > 0:
            self._query_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: query_positions 超时")
        if self.query_delay > 0:
            await asyncio.sleep(self.query_delay)
        return await self._inner.query_positions()

    async def query_order(self, client_order_id: str) -> Order | None:
        if self._query_errors > 0:
            self._query_errors -= 1
            raise RuntimeError(
                f"fault-inject: 券商返回异常数据 (query_order {client_order_id})"
            )
        if self._query_timeouts > 0:
            self._query_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: query_order 超时")
        if self.query_delay > 0:
            await asyncio.sleep(self.query_delay)
        return await self._inner.query_order(client_order_id)

    async def query_active_orders(self) -> list[Order]:
        if self._query_timeouts > 0:
            self._query_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: query_active_orders 超时")
        if self.query_delay > 0:
            await asyncio.sleep(self.query_delay)
        return await self._inner.query_active_orders()

    # ------------------------------------------------------------------ 交易
    async def place_order(self, order: Order) -> SubmissionResult:
        if self._place_timeouts > 0:
            self._place_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: place_order 超时")
        if self.place_delay > 0:
            await asyncio.sleep(self.place_delay)
        if self._place_rejects > 0:
            self._place_rejects -= 1
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=self._place_reject_reason,
            )
        return await self._inner.place_order(order)

    async def cancel_order(self, client_order_id: str) -> None:
        if self._cancel_timeouts > 0:
            self._cancel_timeouts -= 1
            raise BrokerTimeoutError("fault-inject: cancel_order 超时")
        if self.cancel_delay > 0:
            await asyncio.sleep(self.cancel_delay)
        await self._inner.cancel_order(client_order_id)

    # ------------------------------------------------------------------ 事件流
    def events(self) -> AsyncIterator[BrokerEvent]:
        return self._fault_event_iterator()

    async def _fault_event_iterator(self) -> AsyncIterator[BrokerEvent]:
        """先消费注入的事件,再委托内层 broker 的事件流。"""
        # 先把注入的事件推完
        for event in self._pending_events:
            yield event
        self._pending_events.clear()

        # 委托内层事件流
        async for event in self._inner.events():
            yield event
