"""``MockBroker`` 行为单测。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finboard_shared.exceptions import BrokerError
from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Order, Symbol
from finboard_shared.types import (
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    Side,
)


@pytest.mark.unit
async def test_query_account_after_connect(mock_broker, account_id: AccountId) -> None:
    account = await mock_broker.query_account()
    assert account.account_id == account_id
    assert account.broker_kind is BrokerKind.MOCK
    assert account.cash == Decimal("1000000")


@pytest.mark.unit
async def test_place_limit_order_accepted(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    """契约:place_order 不修改传入 order.status,只回 SubmissionResult + 推事件。

    想知道 broker 侧状态必须走 query_order —— 这与真实 broker 一致,
    真实 broker 也不会回写客户端持有的对象。
    """
    order = _build_limit_order(account_id, symbol)
    result = await mock_broker.place_order(order)
    assert result.accepted
    assert result.broker_order_id is not None
    # broker 不应污染传入 order 的 status(状态推进是 OrderManager 的事)
    assert order.status is OrderStatus.CREATED
    assert order.broker_order_id == result.broker_order_id

    # broker 内部状态通过 query_order 查
    snapshot = await mock_broker.query_order(str(order.client_order_id))
    assert snapshot is not None
    assert snapshot.status is OrderStatus.ACKNOWLEDGED


@pytest.mark.unit
async def test_cancel_order_transitions_status(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    await mock_broker.place_order(order)
    await mock_broker.cancel_order(str(order.client_order_id))
    snapshot = await mock_broker.query_order(str(order.client_order_id))
    assert snapshot is not None
    assert snapshot.status is OrderStatus.CANCELLED


@pytest.mark.unit
async def test_cancel_inactive_order_raises(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    await mock_broker.place_order(order)
    await mock_broker.cancel_order(str(order.client_order_id))
    # 重复撤单应抛错
    with pytest.raises(BrokerError):
        await mock_broker.cancel_order(str(order.client_order_id))


@pytest.mark.unit
async def test_match_limit_order_fires_fill(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    await mock_broker.place_order(order)
    await mock_broker.match_limit_order(str(order.client_order_id), Decimal("3.90"))
    snapshot = await mock_broker.query_order(str(order.client_order_id))
    assert snapshot is not None
    assert snapshot.status is OrderStatus.FILLED
    assert snapshot.filled_quantity == Decimal("100")
    assert snapshot.average_fill_price == Decimal("3.90")


@pytest.mark.unit
async def test_duplicate_place_returns_not_accepted(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    first = await mock_broker.place_order(order)
    second = await mock_broker.place_order(order)
    assert first.accepted
    assert not second.accepted


@pytest.mark.unit
async def test_query_active_orders_filters_terminal(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    """已 CANCELLED 的订单不应出现在 query_active_orders 中。"""
    order = _build_limit_order(account_id, symbol)
    await mock_broker.place_order(order)
    await mock_broker.cancel_order(str(order.client_order_id))
    actives = await mock_broker.query_active_orders()
    assert actives == []


def _build_limit_order(account_id: AccountId, symbol: Symbol) -> Order:
    return Order(
        client_order_id=ClientOrderId("F-test-0000000000000001"),
        account_id=account_id,
        broker_kind=BrokerKind.MOCK,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.85"),
    )


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(code="510300.SH", market=Market.A_SHARE)
