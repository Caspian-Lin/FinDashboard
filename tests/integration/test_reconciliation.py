"""核对引擎端到端测试(phase1_doc.md §3.4 步骤 14)。

覆盖:
* 本地 ↔ 券商无差异时 report.ok = True;
* 持仓差异 / 订单差异 / 账户差异被正确检测并写入 reconciliation_logs 表;
* broker_only_orders / local_only_orders 分类正确。

ReconciliationEngine 完整实现已有,这里只做端到端验证。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import MockBroker
from finboard_persistence import (
    AccountRepository,
    OrderRepository,
    PositionRepository,
    ReconciliationLogRepository,
)
from finboard_reconcile import ReconciliationEngine
from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Account, Order, Position, Symbol
from finboard_shared.types import (
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def reconciler(db_session, account_id: AccountId):
    """MockBroker + repos + ReconciliationEngine,broker 已连接。"""
    broker = MockBroker()
    await broker.connect(account_id, {})
    engine = ReconciliationEngine(
        broker=broker,
        account_id=account_id,
        order_repo=OrderRepository(db_session),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        log_repo=ReconciliationLogRepository(db_session),
    )
    yield engine
    await broker.disconnect()


async def test_reconcile_clean_state(reconciler, account_id: AccountId) -> None:
    """本地与 broker 均无持仓 / 无活动订单 → report.ok = True。"""
    # kernel.start 场景下 account 会被 refresh;这里手工写一个本地 account
    # 让 reconcile 能对比(broker query_account 返回初始 1M)
    acct_repo = reconciler._accounts
    await acct_repo.upsert(
        Account(
            account_id=account_id,
            broker_kind=BrokerKind.MOCK,
            total_asset=Decimal("1000000"),
            cash=Decimal("1000000"),
        )
    )
    await db_session_flush(reconciler)

    report = await reconciler.run()
    assert report.ok, report.summary()


async def test_reconcile_detects_position_mismatch(
    reconciler, account_id: AccountId, db_session
) -> None:
    """本地持仓 100 股,broker 无此持仓 → position_mismatches 非空 + 日志入库。"""
    pos_repo = reconciler._positions
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)
    await pos_repo.upsert_local(
        Position(
            account_id=account_id,
            symbol=symbol,
            total_quantity=Decimal("100"),
            available_quantity=Decimal("100"),
            average_price=Decimal("3.85"),
        )
    )
    await db_session_flush(reconciler)

    report = await reconciler.run()
    assert not report.ok
    assert len(report.position_mismatches) == 1
    mismatch = report.position_mismatches[0]
    assert mismatch.symbol == "510300.SH"
    assert mismatch.local_total == Decimal("100")
    assert mismatch.broker_total == Decimal("0")

    # 差异写入 reconciliation_logs
    logs = await _list_recon_logs(db_session, str(account_id), "position")
    assert len(logs) == 1


async def test_reconcile_detects_order_local_only(
    reconciler, account_id: AccountId, db_session
) -> None:
    """本地有活动订单但 broker 查不到(如本地认为 ACKNOWLEDGED,broker 已终态)
    → local_only_orders 非空。"""
    order_repo = reconciler._orders
    cid = ClientOrderId("F-recon-local-only-0001")
    await order_repo.add(
        Order(
            client_order_id=cid,
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
    await db_session_flush(reconciler)

    report = await reconciler.run()
    assert not report.ok
    assert str(cid) in report.local_only_orders


async def test_reconcile_detects_account_mismatch(
    reconciler, account_id: AccountId, db_session
) -> None:
    """本地账户 cash 与 broker cash 差异超过容忍(0.01)→ account_mismatches。"""
    acct_repo = reconciler._accounts
    await acct_repo.upsert(
        Account(
            account_id=account_id,
            broker_kind=BrokerKind.MOCK,
            total_asset=Decimal("1000000"),
            cash=Decimal("500000"),  # broker 是 1M,差 50 万
        )
    )
    await db_session_flush(reconciler)

    report = await reconciler.run()
    assert not report.ok
    cash_mismatch = [
        m for m in report.account_mismatches if m.field_name == "cash"
    ]
    assert len(cash_mismatch) == 1
    assert cash_mismatch[0].diff == Decimal("500000") - Decimal("1000000")


async def test_reconcile_persists_all_log_kinds(
    reconciler, account_id: AccountId, db_session
) -> None:
    """同时存在持仓 + 账户差异时,两类日志都入库。"""
    pos_repo = reconciler._positions
    acct_repo = reconciler._accounts
    symbol = Symbol(code="510300.SH", market=Market.A_SHARE)

    await pos_repo.upsert_local(
        Position(
            account_id=account_id,
            symbol=symbol,
            total_quantity=Decimal("100"),
            available_quantity=Decimal("100"),
            average_price=Decimal("3.85"),
        )
    )
    await acct_repo.upsert(
        Account(
            account_id=account_id,
            broker_kind=BrokerKind.MOCK,
            total_asset=Decimal("1000000"),
            cash=Decimal("500000"),
        )
    )
    await db_session_flush(reconciler)

    report = await reconciler.run()
    assert not report.ok
    assert len(report.position_mismatches) == 1
    assert len(report.account_mismatches) >= 1

    pos_logs = await _list_recon_logs(db_session, str(account_id), "position")
    acct_logs = await _list_recon_logs(db_session, str(account_id), "account")
    assert len(pos_logs) == 1
    assert len(acct_logs) >= 1


# --------------------------------------------------------------------------- helpers
async def db_session_flush(engine: ReconciliationEngine) -> None:
    """reconciler 持有的 repos 共享同一 session;flush 让本地写入对 broker 查询可见。"""
    await engine._positions._session.flush()


async def _list_recon_logs(session, account_id: str, kind: str):
    from sqlalchemy import select

    from finboard_persistence.models import ReconciliationLogModel

    stmt = (
        select(ReconciliationLogModel)
        .where(
            ReconciliationLogModel.account_id == account_id,
            ReconciliationLogModel.kind == kind,
        )
        .order_by(ReconciliationLogModel.id)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


_ = PositionSide  # 防止未使用告警
