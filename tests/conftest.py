"""共享测试夹具。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import MockBroker
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderType, Side


@pytest.fixture
def account_id() -> AccountId:
    return AccountId("test-account")


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(code="510300.SH", market=Market.A_SHARE)


@pytest.fixture
def limit_buy_request(account_id: AccountId, symbol: Symbol) -> OrderRequest:
    return OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.85"),
    )


@pytest_asyncio.fixture
async def mock_broker(account_id: AccountId) -> AsyncIterator[MockBroker]:
    broker = MockBroker()
    await broker.connect(account_id, {})
    yield broker
    await broker.disconnect()
