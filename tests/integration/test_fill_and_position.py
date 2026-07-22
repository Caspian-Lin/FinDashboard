"""成交链路端到端测试(phase1_doc.md §3.4 步骤 7-10, 13)。

覆盖:
* 步骤 7-9:  下限价单 → 撮合 → 接收委托确认 + 成交回报;
* 步骤 10:   成交后查询到新的持仓;
* 步骤 13:   反向卖出交易 → 持仓恢复;
* 额外:     fill 幂等(重复回报不重复入库 / 不双写 filled_quantity)。

前置依赖:MockBroker._fill_order 不再修改传入 Order 的聚合字段
(否则 OrderManager._on_filled 会再累加一次 → 双写)。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import MockBroker
from finboard_core import TradingKernel
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side

pytestmark = pytest.mark.integration


def _get_mock_broker(kernel: TradingKernel) -> MockBroker:
    broker = kernel.order_manager._broker
    assert isinstance(broker, MockBroker), "成交测试需要 MockBroker"
    return broker


@pytest_asyncio.fixture
async def kernel_with_mock(
    db_session, account_id: AccountId
) -> AsyncIterator[TradingKernel]:
    """直接构造 MockBroker(而非 create_broker)以便测试调用 match_limit_order。"""
    broker = MockBroker()
    risk = PreTradeChecker(config=RiskConfig(), kill_switch=KillSwitch())
    kernel = TradingKernel(
        broker=broker,
        account_id=account_id,
        credentials={},
        order_repo=OrderRepository(db_session),
        fill_repo=FillRepository(db_session),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        audit_repo=AuditLogRepository(db_session),
        risk_checker=risk,
    )
    await kernel.start()
    yield kernel
    await kernel.stop()
    await db_session.rollback()


async def test_limit_order_fill_updates_position_and_account(
    kernel_with_mock: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    """步骤 7-10: 限价单成交 → 持仓增加 + 资金减少。"""
    kernel = kernel_with_mock
    broker = _get_mock_broker(kernel)
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    order_repo = kernel.order_manager._orders

    account_before = await kernel.account_manager.current()
    assert account_before is not None
    cash_before = account_before.cash

    # 步骤 7: 发送限价单(挂在不会立即成交的价位)
    request = OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.85"),
    )
    order = await kernel.order_manager.place_order(request)
    cid = str(order.client_order_id)

    # 步骤 8: 接收委托确认 → 等 ACKNOWLEDGED
    await wait_for_status(order_repo, cid, OrderStatus.ACKNOWLEDGED)

    # 步骤 7 cont: 撮合 → 触发成交回报
    await broker.match_limit_order(cid, Decimal("3.90"))

    # 步骤 9: 接收成交回报 → 等 FILLED
    filled = await wait_for_status(order_repo, cid, OrderStatus.FILLED)

    # _on_filled 先 update_state(FILLED) 再 publish(OrderFilled)→apply_fill,
    # 二者之间有 await 让出点;需等 event loop 跑完 consumer task 余下部分
    await asyncio.sleep(0.05)

    # ---- 订单聚合字段正确(无双写) ----
    assert filled.filled_quantity == Decimal("100")
    assert filled.average_fill_price == Decimal("3.90")

    # ---- 步骤 10: 查询到新的持仓 ----
    # 成交事件 → OrderManager._on_filled → publish(OrderFilled)
    #           → kernel._on_order_filled → PositionManager.apply_fill → DB
    positions = await kernel.position_manager.list_local()
    target = [p for p in positions if p.symbol.code == symbol.code]
    assert len(target) == 1
    assert target[0].total_quantity == Decimal("100")
    assert target[0].average_price == Decimal("3.90")

    # ---- fill 入库 ----
    fills = await kernel.order_manager._fills.list_by_order(cid)
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("100")
    assert fills[0].price == Decimal("3.90")

    # ---- 资金变化:买入 100 股 @ 3.90 + 手续费 ----
    await kernel.account_manager.refresh()
    account_after = await kernel.account_manager.current()
    assert account_after is not None
    expected_commission = (Decimal("100") * Decimal("3.90") * Decimal("0.0003")).quantize(
        Decimal("0.01")
    )
    expected_cash = cash_before - Decimal("100") * Decimal("3.90") - expected_commission
    assert account_after.cash == expected_cash


async def test_sell_reduces_position(
    kernel_with_mock: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    """步骤 13: 反向交易 —— 买入建仓后卖出,持仓恢复。"""
    kernel = kernel_with_mock
    broker = _get_mock_broker(kernel)
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    order_repo = kernel.order_manager._orders

    # 1) 买入建仓 200 股
    buy = OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("200"),
        price=Decimal("3.85"),
    )
    buy_order = await kernel.order_manager.place_order(buy)
    buy_cid = str(buy_order.client_order_id)
    await broker.match_limit_order(buy_cid, Decimal("3.85"))
    await wait_for_status(order_repo, buy_cid, OrderStatus.FILLED)
    await asyncio.sleep(0.05)

    positions = await kernel.position_manager.list_local()
    assert any(
        p.symbol.code == symbol.code and p.total_quantity == Decimal("200")
        for p in positions
    )

    # 2) 卖出 200 股(反向交易)
    sell = OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("200"),
        price=Decimal("3.95"),
    )
    sell_order = await kernel.order_manager.place_order(sell)
    sell_cid = str(sell_order.client_order_id)
    await broker.match_limit_order(sell_cid, Decimal("3.95"))
    await wait_for_status(order_repo, sell_cid, OrderStatus.FILLED)
    await asyncio.sleep(0.05)

    # 3) 持仓恢复到 0
    positions_after = await kernel.position_manager.list_local()
    target = [p for p in positions_after if p.symbol.code == symbol.code]
    assert len(target) == 1
    assert target[0].total_quantity == Decimal("0")


async def test_fill_idempotent(
    kernel_with_mock: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    """重复 fill_id 不重复入库,不双写 filled_quantity(幂等兜底)。"""
    from finboard_broker.events import BrokerEvent, BrokerEventType

    kernel = kernel_with_mock
    broker = _get_mock_broker(kernel)
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    order_repo = kernel.order_manager._orders

    request = OrderRequest(
        account_id=account_id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.85"),
    )
    order = await kernel.order_manager.place_order(request)
    cid = str(order.client_order_id)
    await broker.match_limit_order(cid, Decimal("3.85"))
    await wait_for_status(order_repo, cid, OrderStatus.FILLED)

    # 模拟 broker 重放同一笔 fill 事件
    fills = await kernel.order_manager._fills.list_by_order(cid)
    assert len(fills) == 1
    original_fill = fills[0]

    replay = BrokerEvent(
        type=BrokerEventType.ORDER_FILLED,
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        fill=original_fill,
    )
    await kernel.order_manager._handle_broker_event(replay)
    await asyncio.sleep(0)  # 让 publish 回调跑完

    # 仍然只有 1 笔 fill;filled_quantity 没有被双写
    fills_after = await kernel.order_manager._fills.list_by_order(cid)
    assert len(fills_after) == 1
    refreshed = await order_repo.get(cid)
    assert refreshed is not None
    assert refreshed.filled_quantity == Decimal("100")
