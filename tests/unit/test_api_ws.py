"""WebSocket 事件序列化 + ConnectionManager 单元测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from finboard_api.ws import event_to_dict
from finboard_core.events import (
    AccountSnapshotUpdated,
    KillSwitchActivated,
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    PositionUpdated,
)
from finboard_shared.identifiers import AccountId, ClientOrderId, StrategyId
from finboard_shared.models import Account, Fill, Order, Symbol
from finboard_shared.types import (
    BrokerKind,
    KillSwitchLevel,
    Market,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)


def _make_order(
    *,
    status: OrderStatus = OrderStatus.ACKNOWLEDGED,
    side: Side = Side.BUY,
) -> Order:
    return Order(
        client_order_id=ClientOrderId("F-20250101-abc123"),
        account_id=AccountId("test-account"),
        broker_kind=BrokerKind.MOCK,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.50"),
        status=status,
    )


@pytest.mark.unit
def test_event_order_created() -> None:
    order = _make_order()
    event = OrderCreated(order=order)
    data = event_to_dict(event)
    assert data["type"] == "order_created"
    assert data["order"]["client_order_id"] == "F-20250101-abc123"
    assert data["order"]["symbol"] == "510300.SH"
    assert data["order"]["quantity"] == "100"
    assert data["order"]["price"] == "3.50"
    assert "timestamp" in data


@pytest.mark.unit
def test_event_order_filled() -> None:
    order = _make_order(status=OrderStatus.FILLED)
    fill = Fill(
        fill_id="fill-001",
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=Decimal("100"),
        price=Decimal("3.48"),
        commission=Decimal("5.00"),
    )
    event = OrderFilled(order=order, fill=fill)
    data = event_to_dict(event)
    assert data["type"] == "order_filled"
    assert data["fill"]["fill_id"] == "fill-001"
    assert data["fill"]["price"] == "3.48"
    assert data["fill"]["commission"] == "5.00"


@pytest.mark.unit
def test_event_order_cancelled() -> None:
    order = _make_order(status=OrderStatus.CANCELLED)
    data = event_to_dict(OrderCancelled(order=order))
    assert data["type"] == "order_cancelled"
    assert data["order"]["status"] == "cancelled"


@pytest.mark.unit
def test_event_order_rejected() -> None:
    order = _make_order(status=OrderStatus.REJECTED)
    event = OrderRejected(
        order=order, reason=RejectReason.RISK_CHECK_FAILED, message="too large"
    )
    data = event_to_dict(event)
    assert data["type"] == "order_rejected"
    assert data["reason"] == "risk_check_failed"
    assert data["message"] == "too large"


@pytest.mark.unit
def test_event_position_updated() -> None:
    event = PositionUpdated(
        account_id=AccountId("test"),
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        total_quantity=Decimal("200"),
        available_quantity=Decimal("100"),
    )
    data = event_to_dict(event)
    assert data["type"] == "position_updated"
    assert data["symbol"] == "510300.SH"
    assert data["total_quantity"] == "200"
    assert data["available_quantity"] == "100"


@pytest.mark.unit
def test_event_account_snapshot_updated() -> None:
    account = Account(
        account_id=AccountId("test"),
        broker_kind=BrokerKind.MOCK,
        total_asset=Decimal("100000"),
        cash=Decimal("50000"),
    )
    data = event_to_dict(AccountSnapshotUpdated(account=account))
    assert data["type"] == "account_updated"
    assert data["account"]["total_asset"] == "100000"
    assert data["account"]["cash"] == "50000"


@pytest.mark.unit
def test_event_kill_switch_activated() -> None:
    event = KillSwitchActivated(
        level=KillSwitchLevel.NO_NEW_ORDERS,
        strategy_id=StrategyId("etf-dca"),
        reason="manual stop",
    )
    data = event_to_dict(event)
    assert data["type"] == "kill_switch_activated"
    assert data["level"] == "no_new_orders"
    assert data["reason"] == "manual stop"


@pytest.mark.unit
def test_event_json_serializable() -> None:
    """所有事件类型应可直接 json.dumps。"""
    order = _make_order()
    events = [
        OrderCreated(order=order),
        OrderCancelled(order=order),
        OrderRejected(order=order, reason=RejectReason.BROKER_REJECTED),
        KillSwitchActivated(level=KillSwitchLevel.HALT),
        PositionUpdated(
            account_id=AccountId("a"),
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            total_quantity=Decimal("0"),
            available_quantity=Decimal("0"),
        ),
    ]
    for event in events:
        data = event_to_dict(event)
        json.dumps(data, default=str)
