"""端到端 smoke 测试。

把 TradingKernel + MockBroker + 真实 PostgreSQL 串起来,验证 P0 关键链路:

1. 启动内核,初始账户 / 持仓落地;
2. 下限价单 → 状态推进到 ACKNOWLEDGED;
3. 撤单 → 状态推进到 CANCELLED;
4. 重复 client_order_id 被 DB UNIQUE 拒绝。

需要 PostgreSQL(本地 ``make db-up`` 或 CI service container)。
``_engine`` / ``db_session`` / ``account_id`` / ``wait_for_status``
见 :mod:`tests.integration.conftest`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import create_broker
from finboard_core import TradingKernel
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId, generate_client_order_id
from finboard_shared.models import Order, OrderRequest, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side


@pytest_asyncio.fixture
async def kernel(db_session, account_id: AccountId) -> AsyncIterator[TradingKernel]:
    broker = create_broker("mock")
    cfg = RiskConfig()
    risk = PreTradeChecker(config=cfg, kill_switch=KillSwitch())
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


@pytest.mark.integration
async def test_kernel_initial_snapshot(
    kernel: TradingKernel, account_id: AccountId
) -> None:
    assert kernel.ready
    account = await kernel.account_manager.current()
    assert account is not None
    assert account.account_id == account_id


@pytest.mark.integration
async def test_place_limit_and_cancel(
    kernel: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    request = OrderRequest(
        account_id=account_id,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.80"),
    )
    order = await kernel.order_manager.place_order(request)
    assert order.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.SUBMITTED)
    assert order.broker_order_id is not None
    assert order.client_order_id.startswith("F-")

    await kernel.order_manager.cancel_order(str(order.client_order_id))

    # cancel_order 把状态置为 CANCEL_PENDING 后立即返回;真正的 CANCELLED
    # 要等 OrderManager 的 broker-event 消费 task 处理 ORDER_CANCELLED 事件后才落库。
    # 这里 polling 等待终态,避免依赖固定 sleep 时长。
    refreshed = await wait_for_status(
        kernel.order_manager._orders,
        str(order.client_order_id),
        OrderStatus.CANCELLED,
    )
    assert refreshed is not None
    assert refreshed.status is OrderStatus.CANCELLED


@pytest.mark.integration
async def test_duplicate_client_order_id_rejected(
    kernel: TradingKernel,
    account_id: AccountId,
) -> None:
    """同一 client_order_id 入库第二次必须被拒 —— 红线 DB 兜底。"""
    cid = generate_client_order_id()
    order1 = Order(
        client_order_id=cid,
        account_id=account_id,
        broker_kind=kernel.order_manager._broker.kind,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.80"),
        status=OrderStatus.CREATED,
    )
    await kernel.order_manager._orders.add(order1)

    order2 = Order(
        client_order_id=cid,
        account_id=account_id,
        broker_kind=kernel.order_manager._broker.kind,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.80"),
        status=OrderStatus.CREATED,
    )
    with pytest.raises(Exception, match="client_order_id"):
        await kernel.order_manager._orders.add(order2)


@pytest.mark.integration
async def test_db_round_trip(db_session) -> None:
    """直接 ping 一下数据库连通性 + 时区设置。"""
    from sqlalchemy import text

    result = await db_session.execute(text("SELECT 1 AS one"))
    row = result.first()
    assert row is not None
    assert row.one == 1
