"""故障注入端到端测试(phase1_doc.md §9.1)。

覆盖 §9.1 故障场景表:

| 故障场景         | 测试                                    |
|-----------------|-----------------------------------------|
| 连接断开         | test_disconnect_event_logged            |
| 下单请求超时      | test_place_timeout_goes_unknown         |
| 撤单请求超时      | test_cancel_timeout_stays_pending       |
| 重复成交回报      | test_duplicate_fill_idempotent          |
| 订单回报乱序      | test_illegal_transition_rejected        |
| 部分成交后重启    | test_partial_fill_restart_recovery      |
| 券商返回异常数据  | test_query_error_tolerated              |
| broker 拒单      | test_broker_reject_audited              |
| 延迟抖动          | test_network_jitter_tolerated           |
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import FaultInjectionBroker, MockBroker
from finboard_core import TradingKernel
from finboard_core.state_machine import InvalidStateTransitionError
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
from finboard_shared.exceptions import BrokerTimeoutError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side
from tests.integration.conftest import clean_tables

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def fault_kernel(
    db_session, account_id: AccountId
) -> AsyncIterator[tuple[TradingKernel, FaultInjectionBroker, MockBroker]]:
    """带 FaultInjectionBroker 的 kernel。

    返回 (kernel, fault_broker, mock_inner)。
    """
    mock = MockBroker()
    fault = FaultInjectionBroker(mock)
    risk = PreTradeChecker(config=RiskConfig(), kill_switch=KillSwitch())
    order_repo = OrderRepository(db_session)
    fill_repo = FillRepository(db_session)
    kernel = TradingKernel(
        broker=fault,
        account_id=account_id,
        credentials={},
        order_repo=order_repo,
        fill_repo=fill_repo,
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        audit_repo=AuditLogRepository(db_session),
        risk_checker=risk,
    )
    await kernel.start()
    yield kernel, fault, mock
    await kernel.stop()
    await db_session.rollback()


def _buy_request(account_id: AccountId, symbol_code: str = "510300.SH") -> OrderRequest:
    return OrderRequest(
        account_id=account_id,
        symbol=Symbol(code=symbol_code, market=Market.A_SHARE),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        price=Decimal("4.50"),
    )


# ============================================================================
# §9.1 场景: 下单请求超时 → UNKNOWN → 不重试
# ============================================================================
async def test_place_timeout_goes_unknown(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
    wait_for_status,
) -> None:
    """下单超时 → 订单进 UNKNOWN → 不重试 → 审计日志记录 submit_timeout。"""
    kernel, fault, _ = fault_kernel
    fault.inject_place_timeout(times=1)

    with pytest.raises(BrokerTimeoutError):
        await kernel.order_manager.place_order(_buy_request(account_id))

    # 等待状态写入
    await asyncio.sleep(0.05)
    orders = await kernel.order_manager._orders.list_active(str(account_id))
    assert any(o.status is OrderStatus.UNKNOWN for o in orders)

    # 验证审计日志
    logs = await kernel.order_manager._audit.list_recent(limit=20)
    assert any(log.action == "submit_timeout" for log in logs)


# ============================================================================
# §9.1 场景: 撤单请求超时 → CANCEL_PENDING 保持
# ============================================================================
async def test_cancel_timeout_stays_pending(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
    wait_for_status,
) -> None:
    """撤单超时 → 订单保持 CANCEL_PENDING → 审计日志记录 cancel_timeout。"""
    kernel, fault, _ = fault_kernel

    # 先正常下单
    order = await kernel.order_manager.place_order(_buy_request(account_id))
    await wait_for_status(
        kernel.order_manager._orders, str(order.client_order_id), OrderStatus.ACKNOWLEDGED
    )

    # 注入撤单超时
    fault.inject_cancel_timeout(times=1)
    with pytest.raises(BrokerTimeoutError):
        await kernel.order_manager.cancel_order(str(order.client_order_id))

    # 订单应保持 CANCEL_PENDING
    await asyncio.sleep(0.05)
    db_order = await kernel.order_manager._orders.get(str(order.client_order_id))
    assert db_order is not None
    assert db_order.status is OrderStatus.CANCEL_PENDING

    # 审计日志
    logs = await kernel.order_manager._audit.list_recent(limit=20)
    assert any(log.action == "cancel_timeout" for log in logs)


# ============================================================================
# §9.1 场景: 重复成交回报 → fill_id 幂等去重
# ============================================================================
async def test_duplicate_fill_idempotent(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
    wait_for_status,
) -> None:
    """同一 fill_id 的回报重放 → 不重复入库 / 不双写 filled_quantity。"""
    kernel, _, mock = fault_kernel

    order = await kernel.order_manager.place_order(_buy_request(account_id))
    await wait_for_status(
        kernel.order_manager._orders, str(order.client_order_id), OrderStatus.ACKNOWLEDGED
    )

    # 正常撮合
    await mock.match_limit_order(str(order.client_order_id), Decimal("4.50"))
    await wait_for_status(
        kernel.order_manager._orders, str(order.client_order_id), OrderStatus.FILLED
    )

    fill_repo = kernel.order_manager._fills
    fills_before, _ = await fill_repo.list_by_account(str(account_id))
    assert len(fills_before) == 1

    # 重放同一个 fill 事件
    original_fill = fills_before[0]
    from finboard_broker.events import BrokerEvent, BrokerEventType
    from finboard_shared.models import Fill

    replay = BrokerEvent(
        type=BrokerEventType.ORDER_FILLED,
        client_order_id=order.client_order_id,
        fill=Fill(
            fill_id=original_fill.fill_id,
            client_order_id=original_fill.client_order_id,
            broker_order_id=original_fill.broker_order_id,
            symbol=original_fill.symbol,
            side=original_fill.side,
            quantity=original_fill.quantity,
            price=original_fill.price,
            commission=original_fill.commission,
        ),
    )
    await kernel.order_manager._handle_broker_event(replay)
    await asyncio.sleep(0.02)

    # 验证去重: fill 数量不变
    fills_after, _ = await fill_repo.list_by_account(str(account_id))
    assert len(fills_after) == 1


# ============================================================================
# §9.1 场景: 订单回报乱序 → 状态机拒绝非法迁移
# ============================================================================
async def test_illegal_transition_rejected(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
) -> None:
    """FILLED → CANCELLED 非法迁移被状态机拒绝。"""
    kernel, _, _ = fault_kernel

    order = await kernel.order_manager.place_order(_buy_request(account_id))

    # 手动把订单状态推到 FILLED(绕过正常流程)
    order.status = OrderStatus.FILLED
    order.filled_quantity = order.quantity

    # 尝试从 FILLED 转到 CANCELLED → 应抛异常
    with pytest.raises(InvalidStateTransitionError):
        await kernel.order_manager._transition(order, OrderStatus.CANCELLED)


# ============================================================================
# §9.1 场景: 券商返回异常数据 → 容错
# ============================================================================
async def test_query_error_tolerated(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
) -> None:
    """query_order 抛异常 → 系统不崩溃(事件消费循环继续)。"""
    kernel, fault, _ = fault_kernel

    # 注入查询异常
    fault.inject_query_error(times=1)

    # query_order 抛异常
    with pytest.raises(RuntimeError):
        await kernel.broker.query_order("nonexistent")

    # 系统仍然可以正常下单
    fault.inject_query_error(times=0)  # 清除故障
    order = await kernel.order_manager.place_order(_buy_request(account_id))
    assert order.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.SUBMITTED)


# ============================================================================
# §9.1 场景: 连接断开 → DISCONNECTED 事件处理
# ============================================================================
async def test_disconnect_event_logged(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
) -> None:
    """注入 DISCONNECTED 事件 → OrderManager 日志记录 → 不崩溃。"""
    kernel, _fault, _ = fault_kernel

    from finboard_broker.events import BrokerEvent, BrokerEventType

    # 直接调用 _handle_broker_event 模拟收到断连事件
    disconnect_event = BrokerEvent(type=BrokerEventType.DISCONNECTED)
    await kernel.order_manager._handle_broker_event(disconnect_event)

    # 系统仍然运行(不崩溃)
    order = await kernel.order_manager.place_order(_buy_request(account_id))
    assert order.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.SUBMITTED)


# ============================================================================
# §9.1 场景: broker 拒单 → REJECTED 状态 + 审计日志
# ============================================================================
async def test_broker_reject_audited(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
    wait_for_status,
) -> None:
    """broker 立即拒单 → 订单进 REJECTED → 审计日志记录 order_rejected。"""
    kernel, fault, _ = fault_kernel
    fault.inject_reject(times=1)

    order = await kernel.order_manager.place_order(_buy_request(account_id))
    assert order.status is OrderStatus.REJECTED

    # 验证审计日志
    logs = await kernel.order_manager._audit.list_recent(limit=20)
    assert any(log.action == "order_rejected" for log in logs)


# ============================================================================
# §9.1 场景: 网络抖动(延迟) → 系统仍可正常工作
# ============================================================================
async def test_network_jitter_tolerated(
    fault_kernel: tuple[TradingKernel, FaultInjectionBroker, MockBroker],
    account_id: AccountId,
    wait_for_status,
) -> None:
    """注入延迟 → 下单链路仍正确完成。"""
    kernel, fault, mock = fault_kernel
    fault.place_delay = 0.05

    order = await kernel.order_manager.place_order(_buy_request(account_id))
    await wait_for_status(
        kernel.order_manager._orders, str(order.client_order_id), OrderStatus.ACKNOWLEDGED
    )

    fault.place_delay = 0.0  # 恢复
    await mock.match_limit_order(str(order.client_order_id), Decimal("4.50"))
    await wait_for_status(
        kernel.order_manager._orders, str(order.client_order_id), OrderStatus.FILLED
    )


# ============================================================================
# §9.1 场景: 部分成交后重启 → 恢复匹配券商状态
# ============================================================================
async def test_partial_fill_restart_recovery(account_id: AccountId) -> None:
    """部分成交 → 重启 → RecoveryEngine 匹配券商状态修复。"""
    from tests.integration.conftest import DB_URL, ensure_test_db

    mock = MockBroker()
    risk = PreTradeChecker(config=RiskConfig(), kill_switch=KillSwitch())

    await ensure_test_db(DB_URL)
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        await clean_tables(conn)

    smaker = session_factory(engine)
    try:
        # 第一轮: 下单 + 部分成交
        async with smaker() as session:
            kernel1 = TradingKernel(
                broker=mock,
                account_id=account_id,
                credentials={},
                order_repo=OrderRepository(session),
                fill_repo=FillRepository(session),
                position_repo=PositionRepository(session),
                account_repo=AccountRepository(session),
                audit_repo=AuditLogRepository(session),
                risk_checker=risk,
            )
            await kernel1.start()

            req = OrderRequest(
                account_id=account_id,
                symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("500"),
                price=Decimal("4.50"),
            )
            order = await kernel1.order_manager.place_order(req)

            await mock.match_limit_order_partial(
                str(order.client_order_id), Decimal("4.50"), Decimal("200")
            )
            await asyncio.sleep(0.1)

            db_order = await OrderRepository(session).get(str(order.client_order_id))
            assert db_order is not None
            assert db_order.status is OrderStatus.PARTIALLY_FILLED
            assert db_order.filled_quantity == Decimal("200")

            await kernel1.stop()
            await session.commit()

        # 第二轮: 重启 → RecoveryEngine 查券商 → 匹配状态
        async with smaker() as session:
            kernel2 = TradingKernel(
                broker=mock,
                account_id=account_id,
                credentials={},
                order_repo=OrderRepository(session),
                fill_repo=FillRepository(session),
                position_repo=PositionRepository(session),
                account_repo=AccountRepository(session),
                audit_repo=AuditLogRepository(session),
                risk_checker=risk,
                recoverer=RecoveryEngine(
                    broker=mock,
                    account_id=account_id,
                    order_repo=OrderRepository(session),
                    audit_repo=AuditLogRepository(session),
                ),
                reconciler=ReconciliationEngine(
                    broker=mock,
                    account_id=account_id,
                    order_repo=OrderRepository(session),
                    position_repo=PositionRepository(session),
                    account_repo=AccountRepository(session),
                    log_repo=ReconciliationLogRepository(session),
                ),
            )
            await kernel2.start()
            assert kernel2.ready is True

            recovered = await OrderRepository(session).get(str(order.client_order_id))
            assert recovered is not None
            assert recovered.status is OrderStatus.PARTIALLY_FILLED
            assert recovered.filled_quantity == Decimal("200")

            await kernel2.stop()
            await session.rollback()
    finally:
        async with engine.begin() as conn:
            await clean_tables(conn)
        await engine.dispose()
