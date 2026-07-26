"""重启恢复引擎集成测试(phase1_doc.md §5.4 步骤 7-8)。

测试场景:
1. UNKNOWN + 券商有(ACKNOWLEDGED) → 修复为 ACKNOWLEDGED;
2. UNKNOWN + 券商无 → REJECTED(安全终止,不重发);
3. SUBMITTING + 券商无 → REJECTED;
4. UNKNOWN + 券商有(FILLED) → 修复为 FILLED + 同步 filled_quantity;
5. 干净恢复(无活动订单)→ ok=True,无修复;
6. 全链路:kernel.start 带 recovery → UNKNOWN 被修复 → reconcile 通过 → ready=True。

红线验证:
* 券商无记录的 UNKNOWN 订单标 REJECTED 后,不再自动重发;
* 恢复以券商为真值同步状态。
"""

from __future__ import annotations

from dataclasses import replace
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
from finboard_reconcile import ReconciliationEngine, RecoveryEngine
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Order, Symbol
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


# --------------------------------------------------------------------------- helpers
def _make_order(
    cid: str,
    account_id: AccountId,
    *,
    status: OrderStatus = OrderStatus.UNKNOWN,
    filled_quantity: Decimal = Decimal("0"),
    broker_order_id: str | None = None,
) -> Order:
    return Order(
        client_order_id=ClientOrderId(cid),
        account_id=account_id,
        broker_kind=BrokerKind.MOCK,
        symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("3.80"),
        time_in_force=TimeInForce.GFD,
        status=status,
        filled_quantity=filled_quantity,
        broker_order_id=broker_order_id,
    )


def _make_recovery(broker, account_id, session) -> RecoveryEngine:
    return RecoveryEngine(
        broker=broker,
        account_id=account_id,
        order_repo=OrderRepository(session),
        audit_repo=AuditLogRepository(session),
    )


# --------------------------------------------------------------------------- tests: RecoveryEngine 直接调用
async def test_unknown_broker_has_acknowledged(db_session, account_id: AccountId) -> None:
    """UNKNOWN + 券商 ACKNOWLEDGED → 修复为 ACKNOWLEDGED。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    order = _make_order("F-recovery-ack-0001", account_id)
    repo = OrderRepository(db_session)
    await repo.add(order)
    await db_session.flush()

    # 券商侧:订单存在且为 ACKNOWLEDGED
    cid = str(order.client_order_id)
    broker._orders[cid] = replace(order)
    broker._broker_status[cid] = OrderStatus.ACKNOWLEDGED

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 1
    repair = report.repairs[0]
    assert repair.from_status == OrderStatus.UNKNOWN
    assert repair.to_status == OrderStatus.ACKNOWLEDGED

    repaired = await repo.get(cid)
    assert repaired is not None
    assert repaired.status == OrderStatus.ACKNOWLEDGED


async def test_unknown_broker_missing_rejected(db_session, account_id: AccountId) -> None:
    """UNKNOWN + 券商无记录 → REJECTED(安全终止,不重发)。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    order = _make_order("F-recovery-missing-001", account_id)
    repo = OrderRepository(db_session)
    await repo.add(order)
    await db_session.flush()

    # 券商侧:不注册此订单 → query_order 返回 None

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 1
    repair = report.repairs[0]
    assert repair.from_status == OrderStatus.UNKNOWN
    assert repair.to_status == OrderStatus.REJECTED

    repaired = await repo.get(str(order.client_order_id))
    assert repaired is not None
    assert repaired.status == OrderStatus.REJECTED


async def test_submitting_broker_missing_rejected(
    db_session, account_id: AccountId
) -> None:
    """SUBMITTING + 券商无记录 → REJECTED(崩溃发生在 broker 接收之前)。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    order = _make_order(
        "F-recovery-submit-001", account_id, status=OrderStatus.SUBMITTING
    )
    repo = OrderRepository(db_session)
    await repo.add(order)
    await db_session.flush()

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 1
    repair_result = report.repairs[0]
    assert repair_result.from_status == OrderStatus.SUBMITTING
    assert repair_result.to_status == OrderStatus.REJECTED


async def test_unknown_broker_filled_sync_quantity(
    db_session, account_id: AccountId
) -> None:
    """UNKNOWN + 券商已 FILLED → 修复为 FILLED + 同步 filled_quantity。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    order = _make_order("F-recovery-filled-001", account_id)
    repo = OrderRepository(db_session)
    await repo.add(order)
    await db_session.flush()

    cid = str(order.client_order_id)
    broker._orders[cid] = replace(order)
    broker._broker_status[cid] = OrderStatus.FILLED
    broker._broker_filled_qty[cid] = Decimal("100")
    broker._broker_avg_price[cid] = Decimal("3.80")

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 1
    repair = report.repairs[0]
    assert repair.to_status == OrderStatus.FILLED
    assert repair.synced_filled_quantity == Decimal("100")

    repaired = await repo.get(cid)
    assert repaired is not None
    assert repaired.status == OrderStatus.FILLED
    assert repaired.filled_quantity == Decimal("100")


async def test_clean_recovery_no_active_orders(
    db_session, account_id: AccountId
) -> None:
    """无活动订单 → 恢复直接完成,ok=True。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 0


async def test_acknowledged_consistent_no_repair(
    db_session, account_id: AccountId
) -> None:
    """本地与券商状态一致 → 不做修复。"""
    broker = MockBroker()
    await broker.connect(account_id, {})

    order = _make_order(
        "F-recovery-consist-001",
        account_id,
        status=OrderStatus.ACKNOWLEDGED,
    )
    repo = OrderRepository(db_session)
    await repo.add(order)
    await db_session.flush()

    cid = str(order.client_order_id)
    broker._orders[cid] = replace(order)
    broker._broker_status[cid] = OrderStatus.ACKNOWLEDGED

    recovery = _make_recovery(broker, account_id, db_session)
    report = await recovery.run()

    assert report.ok
    assert len(report.repairs) == 0


# --------------------------------------------------------------------------- tests: 全链路 kernel 重启恢复
@pytest_asyncio.fixture
async def restart_engine():
    """独立 engine + 建表,跨 session 持久化(测重启恢复必须 commit)。"""
    from tests.integration.conftest import DB_URL

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        await clean_tables(conn)
    yield engine
    async with engine.begin() as conn:
        await clean_tables(conn)
    await engine.dispose()


def _make_kernel(
    broker, account_id, session, *, reconciler=None, recoverer=None
) -> TradingKernel:
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
        recoverer=recoverer,
    )


def _make_reconciler(broker, account_id, session) -> ReconciliationEngine:
    return ReconciliationEngine(
        broker=broker,
        account_id=account_id,
        order_repo=OrderRepository(session),
        position_repo=PositionRepository(session),
        account_repo=AccountRepository(session),
        log_repo=ReconciliationLogRepository(session),
    )


async def test_kernel_recovery_unknown_then_ready(
    restart_engine, account_id: AccountId
) -> None:
    """全链路:UNKNOWN 订单 → 重启 → 恢复修复 → reconcile 通过 → ready=True。

    场景:
    1. Session 1: 放入一笔 UNKNOWN 订单(模拟超时崩溃遗留),券商无此单;
    2. Session 2: 启动 kernel → recovery 修复 UNKNOWN→REJECTED → reconcile 通过 → ready。
    """
    broker = MockBroker()
    smaker = session_factory(restart_engine)

    # Session 1: 写入 UNKNOWN 订单,commit
    async with smaker() as session:
        repo = OrderRepository(session)
        await repo.add(
            _make_order("F-kernel-recovery-001", account_id, status=OrderStatus.UNKNOWN)
        )
        await session.commit()

    # Session 2: 重启 kernel(带 recovery + reconcile)
    async with smaker() as session:
        recovery = _make_recovery(broker, account_id, session)
        recon = _make_reconciler(broker, account_id, session)
        kernel = _make_kernel(
            broker, account_id, session, reconciler=recon, recoverer=recovery
        )
        await kernel.start()

        assert kernel.ready, "恢复修复 UNKNOWN→REJECTED 后应 ready"

        # 验证订单已被修复为 REJECTED(不再活动)
        repo = OrderRepository(session)
        repaired = await repo.get("F-kernel-recovery-001")
        assert repaired is not None
        assert repaired.status == OrderStatus.REJECTED

        await kernel.stop()
        await session.rollback()


async def test_kernel_recovery_no_recoverer_backward_compat(
    db_session, account_id: AccountId
) -> None:
    """不注入 recoverer 时(向后兼容)kernel 仍可正常启动。"""
    broker = MockBroker()
    kernel = _make_kernel(broker, account_id, db_session)
    await kernel.start()
    assert kernel.ready
    await kernel.stop()
