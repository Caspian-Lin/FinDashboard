"""FaultInjectionBroker 单元测试。

验证装饰器的故障注入机制本身(不需要 DB / kernel):
* 超时注入(place / cancel / query)
* 拒单注入
* 查询异常注入
* 断连事件注入
* 延迟注入
* 故障到期后恢复正常委托
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal

import pytest

from finboard_broker import FaultInjectionBroker, MockBroker
from finboard_broker.events import BrokerEvent
from finboard_shared.exceptions import BrokerTimeoutError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Order, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side

pytestmark = pytest.mark.unit


async def _make_broker() -> FaultInjectionBroker:
    """创建已连接的 FaultInjectionBroker(MockBroker)。"""
    mock = MockBroker()
    await mock.connect(AccountId("test-fault"), {})
    return FaultInjectionBroker(mock)


def _make_order() -> Order:
    return Order(
        client_order_id="cid-fault-001",  # type: ignore[arg-type]
        account_id=AccountId("test-fault"),
        broker_kind=MockBroker().kind,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("4.50"),
        status=OrderStatus.RISK_CHECKED,
    )


async def test_place_timeout_injection() -> None:
    """inject_place_timeout 使 place_order 抛 BrokerTimeoutError。"""
    broker = await _make_broker()
    broker.inject_place_timeout(times=1)
    with pytest.raises(BrokerTimeoutError, match="place_order"):
        await broker.place_order(_make_order())


async def test_place_timeout_expires() -> None:
    """故障到期后 place_order 恢复正常委托。"""
    broker = await _make_broker()
    broker.inject_place_timeout(times=1)
    order = _make_order()
    # 第一次触发超时
    with pytest.raises(BrokerTimeoutError):
        await broker.place_order(order)
    # 第二次正常执行
    result = await broker.place_order(order)
    assert result.accepted is True


async def test_cancel_timeout_injection() -> None:
    """inject_cancel_timeout 使 cancel_order 抛 BrokerTimeoutError。"""
    broker = await _make_broker()
    order = _make_order()
    await broker.place_order(order)
    broker.inject_cancel_timeout(times=1)
    with pytest.raises(BrokerTimeoutError, match="cancel_order"):
        await broker.cancel_order(str(order.client_order_id))


async def test_query_error_injection() -> None:
    """inject_query_error 使 query_order 抛非超时异常。"""
    broker = await _make_broker()
    order = _make_order()
    await broker.place_order(order)
    broker.inject_query_error(times=1)
    with pytest.raises(RuntimeError, match="异常数据"):
        await broker.query_order(str(order.client_order_id))
    # 故障到期后正常返回
    result = await broker.query_order(str(order.client_order_id))
    assert result is not None


async def test_reject_injection() -> None:
    """inject_reject 使 place_order 返回 accepted=False。"""
    broker = await _make_broker()
    broker.inject_reject(times=1)
    result = await broker.place_order(_make_order())
    assert result.accepted is False
    assert result.reject_reason is not None


async def test_disconnect_event_injection() -> None:
    """inject_disconnect 向事件流注入 DISCONNECTED 事件。"""
    broker = await _make_broker()
    broker.inject_disconnect()

    events: list[BrokerEvent] = []
    consumer_task = asyncio.create_task(_collect_events(broker, events))
    await asyncio.sleep(0.1)
    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task

    assert any(e.type.value == "disconnected" for e in events)


async def test_delay_injection() -> None:
    """delay 配置使方法调用延迟指定时间。"""
    broker = await _make_broker()
    broker.place_delay = 0.1
    import time

    start = time.monotonic()
    await broker.place_order(_make_order())
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08  # 允许微小误差


async def test_normal_delegation() -> None:
    """不注入任何故障时,FaultInjectionBroker 完全透明委托。"""
    broker = await _make_broker()
    order = _make_order()
    result = await broker.place_order(order)
    assert result.accepted is True
    assert result.broker_order_id is not None

    queried = await broker.query_order(str(order.client_order_id))
    assert queried is not None

    # cancel_order succeeds silently (returns None), failure raises exception
    await broker.cancel_order(str(order.client_order_id))


# --------------------------------------------------------------------------- 辅助
async def _collect_events(
    broker: FaultInjectionBroker, sink: list[BrokerEvent]
) -> None:
    """消费 broker 事件流并追加到 sink。"""
    async for event in broker.events():
        sink.append(event)
