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
    order = _build_limit_order(account_id, symbol)
    result = await mock_broker.place_order(order)
    assert result.accepted
    assert result.broker_order_id is not None
    assert order.status is OrderStatus.ACKNOWLEDGED
    assert order.broker_order_id == result.broker_order_id
    # 回报队列里应该有 CONNECTED + ORDER_ACCEPTED
    # (这里不直接 drain events(),仅校验内部状态)


@pytest.mark.unit
async def test_cancel_order_transitions_status(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    await mock_broker.place_order(order)
    await mock_broker.cancel_order(str(order.client_order_id))
    assert order.status is OrderStatus.CANCELLED


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
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal("100")
    assert order.average_fill_price == Decimal("3.90")


@pytest.mark.unit
async def test_duplicate_place_returns_not_accepted(
    mock_broker, account_id: AccountId, symbol: Symbol
) -> None:
    order = _build_limit_order(account_id, symbol)
    first = await mock_broker.place_order(order)
    second = await mock_broker.place_order(order)
    assert first.accepted
    assert not second.accepted


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
