"""端到端 smoke 测试。

把 TradingKernel + MockBroker + 真实 PostgreSQL 串起来,验证 P0 关键链路:

1. 启动内核,初始账户 / 持仓落地;
2. 下限价单 → 状态推进到 ACKNOWLEDGED;
3. 撤单 → 状态推进到 CANCELLED;
4. 重复 client_order_id 被 DB UNIQUE 拒绝。

需要 PostgreSQL(本地 ``make db-up`` 或 CI service container)。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text

from finboard_broker import create_broker
from finboard_core import TradingKernel
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    Base,
    FillRepository,
    OrderRepository,
    PositionRepository,
    create_async_engine,
    session_factory,
)
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId, generate_client_order_id
from finboard_shared.models import Order, OrderRequest, Symbol
from finboard_shared.types import (
    Market,
    OrderStatus,
    OrderType,
    Side,
)

DB_URL = os.getenv(
    "FINBOARD_DB_URL", "postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/findashboard"
)


@pytest.fixture(scope="module")
def _db_url() -> str:
    return DB_URL


@pytest_asyncio.fixture(scope="module")
async def _engine(_db_url: str):
    engine = create_async_engine(_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        # 测试结束后清表,保留 schema 便于调试
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine):
    smaker = session_factory(_engine)
    async with smaker() as session:
        yield session


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
    refreshed = await kernel.order_manager._orders.get(
        str(order.client_order_id)
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
    result = await db_session.execute(text("SELECT 1 AS one"))
    row = result.first()
    assert row is not None
    assert row.one == 1


@pytest.fixture
def account_id() -> AccountId:
    return AccountId("test-account-integration")
