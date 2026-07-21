"""``client_order_id`` 与状态机 / 领域模型的纯函数测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finboard_core.state_machine import InvalidStateTransitionError, OrderStateMachine
from finboard_shared.identifiers import AccountId, generate_client_order_id
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side


# ----------------------------------------------------------------- client_order_id
def test_client_order_id_format() -> None:
    cid = generate_client_order_id()
    assert cid.startswith("F-")
    # YYYYMMDD-UTC + 16 hex chars
    parts = cid.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8
    assert len(parts[2]) == 16
    int(parts[2], 16)  # 16 hex 必须可解析


def test_client_order_id_unique_in_batch() -> None:
    batch = {generate_client_order_id() for _ in range(10_000)}
    assert len(batch) == 10_000


# ----------------------------------------------------------------- OrderRequest
def test_order_request_requires_price_for_limit(symbol: Symbol) -> None:
    with pytest.raises(ValueError, match="price"):
        OrderRequest(
            account_id=AccountId("x"),
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            price=None,
        )


def test_order_request_market_allows_no_price(symbol: Symbol) -> None:
    req = OrderRequest(
        account_id=AccountId("x"),
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("100"),
        price=None,
    )
    assert req.time_in_force.value == "GFD"


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(code="510300.SH", market=Market.A_SHARE)


# ----------------------------------------------------------------- 状态机
def test_state_machine_legal_transitions() -> None:
    sm = OrderStateMachine
    sm.check_transition(OrderStatus.CREATED, OrderStatus.RISK_CHECKED)
    sm.check_transition(OrderStatus.RISK_CHECKED, OrderStatus.SUBMITTING)
    sm.check_transition(
        OrderStatus.SUBMITTING, OrderStatus.UNKNOWN
    )  # 超时红线 → UNKNOWN
    sm.check_transition(OrderStatus.ACKNOWLEDGED, OrderStatus.FILLED)
    sm.check_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED)


def test_state_machine_illegal_transitions() -> None:
    sm = OrderStateMachine
    # 终态不允许再迁移
    with pytest.raises(InvalidStateTransitionError):
        sm.check_transition(OrderStatus.FILLED, OrderStatus.ACKNOWLEDGED)
    with pytest.raises(InvalidStateTransitionError):
        sm.check_transition(OrderStatus.CANCELLED, OrderStatus.SUBMITTING)
    # 跳级
    with pytest.raises(InvalidStateTransitionError):
        sm.check_transition(OrderStatus.CREATED, OrderStatus.SUBMITTED)


def test_state_machine_unknown_cannot_resubmit() -> None:
    """超时进入 UNKNOWN 后不能直接重新进入 SUBMITTING —— 红线。"""
    sm = OrderStateMachine
    with pytest.raises(InvalidStateTransitionError):
        sm.check_transition(OrderStatus.UNKNOWN, OrderStatus.SUBMITTING)


def test_state_machine_idempotent() -> None:
    OrderStateMachine.check_transition(OrderStatus.FILLED, OrderStatus.FILLED)
