"""重启恢复 + reconcile gate 端到端测试(phase1_doc.md §3.4 步骤 11-12)。

红线(AGENTS.md):
* 系统重启后必须先完成本地 ↔ 券商核对,核对通过前**禁止发送新订单**;
* 核对未通过时 ``kernel.ready`` 保持 False,``OrderManager.place_order`` 抛
  :class:`KernelNotReadyError`。

验证场景:
1. 干净重启(本地=broker)→ ``ready=True``,可下单;
2. 本地有遗留活动订单 broker 没有(如崩溃前未同步)→ ``ready=False``,
   下单被 gate 拒;
3. 跑一次 reconcile 修复 / 人工清理后重新 ``start`` → ``ready=True``。

测试隔离:跨 session commit 真实持久化(不复用 transaction-rollback fixture)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import MockBroker
from finboard_core import TradingKernel
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    Base,
    FillRepository,
    OrderRepository,
    PositionRepository,
    ReconciliationLogRepository,
    create_async_engine,
    session_factory,
)
from finboard_reconcile import ReconciliationEngine
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.exceptions import KernelNotReadyError
from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Order, OrderRequest, Symbol
from finboard_shared.types import (
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from tests.integration.conftest import clean_tables

pytestmark = pytest.mark.integration


def _make_reconciler(broker, account_id, session) -> ReconciliationEngine:
    return ReconciliationEngine(
        broker=broker,
        account_id=account_id,
        order_repo=OrderRepository(session),
        position_repo=PositionRepository(session),
        account_repo=AccountRepository(session),
        log_repo=ReconciliationLogRepository(session),
    )


def _make_kernel(broker, account_id, session, *, reconciler=None) -> TradingKernel:
    return TradingKernel(
        broker=broker,
        account_id=account_id,
        credentials={},
        order_repo=OrderRepository(session),
        fill_repo=FillRepository(session),
        position_repo=PositionRepository(session),
        account_repo=AccountRepository(session),
        audit_repo=AuditLogRepository(session),
        risk_checker=PreTradeChecker(config=RiskConfig(), kill_switch=KillSwitch()),
        reconciler=reconciler,
    )


@pytest_asyncio.fixture
async def restart_engine():
    """独立 engine + 建表,跨 session 持久化(测重启恢复必须 commit)。"""
    from tests.integration.conftest import DB_URL, ensure_test_db

    await ensure_test_db(DB_URL)
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 清表(防止前次遗留)
    async with engine.begin() as conn:
        await clean_tables(conn)
    yield engine
    async with engine.begin() as conn:
        await clean_tables(conn)
    await engine.dispose()


async def test_clean_restart_allows_trading(restart_engine, account_id: AccountId) -> None:
    """步骤 11-12(干净场景):重启 → 核对通过 → ready → 可下单。"""
    broker = MockBroker()
    smaker = session_factory(restart_engine)

    # 第一次 session:启动 → 确认 clean → stop → commit
    async with smaker() as session:
        kernel = _make_kernel(
            broker,
            account_id,
            session,
            reconciler=_make_reconciler(broker, account_id, session),
        )
        await kernel.start()
        assert kernel.ready, "干净状态重启应就绪"
        await kernel.stop()
        await session.commit()

    # 第二次 session:模拟"重启"
    async with smaker() as session:
        kernel2 = _make_kernel(
            broker,
            account_id,
            session,
            reconciler=_make_reconciler(broker, account_id, session),
        )
        await kernel2.start()
        assert kernel2.ready, "重启后核对通过应就绪"

        # 核对通过 → 可下单(不被 gate 拒)
        request = OrderRequest(
            account_id=account_id,
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            price=Decimal("3.80"),
        )
        order = await kernel2.order_manager.place_order(request)
        assert order.client_order_id.startswith("F-")
        await kernel2.stop()
        await session.rollback()


async def test_mismatched_state_blocks_trading(
    restart_engine, account_id: AccountId
) -> None:
    """本地有遗留活动订单 broker 没有(崩溃前未同步)→ 核对失败 → 禁止下单。"""
    broker = MockBroker()
    smaker = session_factory(restart_engine)

    # 第一次 session:写入一笔本地活动订单(模拟崩溃前遗留),commit
    async with smaker() as session:
        repo = OrderRepository(session)
        await repo.add(
            Order(
                client_order_id=ClientOrderId("F-restart-leftover-0001"),
                account_id=account_id,
                broker_kind=BrokerKind.MOCK,
                symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("100"),
                price=Decimal("3.85"),
                time_in_force=TimeInForce.GFD,
                status=OrderStatus.ACKNOWLEDGED,
            )
        )
        await session.commit()

    # 第二次 session:重启 → 核对发现本地有订单 broker 没有 → ready=False
    async with smaker() as session:
        kernel = _make_kernel(
            broker,
            account_id,
            session,
            reconciler=_make_reconciler(broker, account_id, session),
        )
        await kernel.start()
        assert not kernel.ready, "本地有遗留订单 broker 没有时应禁止交易"

        # gate 拒绝下单
        request = OrderRequest(
            account_id=account_id,
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            price=Decimal("3.80"),
        )
        with pytest.raises(KernelNotReadyError):
            await kernel.order_manager.place_order(request)
        await kernel.stop()
        await session.rollback()


async def test_gate_rejects_before_start(account_id: AccountId, db_session) -> None:
    """未调 start(或 start 未完成)时 place_order 必须被拒绝。"""
    broker = MockBroker()
    # 不注入 reconciler,但也不调 start —— _ready 默认 False
    kernel = _make_kernel(broker, account_id, db_session)
    # 注意:没有 await kernel.start()
    assert not kernel.ready

    request = OrderRequest(
        account_id=account_id,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.80"),
    )
    with pytest.raises(KernelNotReadyError):
        await kernel.order_manager.place_order(request)
