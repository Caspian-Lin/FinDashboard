"""定时任务调度器集成测试。

测试覆盖:
* 收盘撤单任务(cancel_all_active)撤掉所有活动订单
* 日终核对任务发现差异时激活 Kill Switch
* 心跳任务检测断连并激活 Kill Switch
* 盘前检查任务(broker 未连接 → Kill Switch)
* Scheduler trigger 手动触发内置任务
* 完整 _run_kernel 风格集成(scheduler + kernel)
"""

from __future__ import annotations

import asyncio
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
    ReconciliationLogRepository,
)
from finboard_reconcile import ReconciliationEngine
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_scheduler import (
    Scheduler,
    TradingCalendar,
    create_default_tasks,
)
from finboard_scheduler.tasks import (
    close_cancel_task,
    end_of_day_reconcile_task,
    heartbeat_task,
    pre_market_check_task,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import KillSwitchLevel, Market, OrderStatus, OrderType, Side


@pytest_asyncio.fixture
async def kernel_with_reconciler(
    db_session, account_id: AccountId
) -> AsyncIterator[TradingKernel]:
    """带 reconciler 的 kernel(模拟生产配置)。"""
    broker = create_broker("mock")
    cfg = RiskConfig()
    risk = PreTradeChecker(config=cfg, kill_switch=KillSwitch())
    reconciler = ReconciliationEngine(
        broker=broker,
        account_id=account_id,
        order_repo=OrderRepository(db_session),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        log_repo=ReconciliationLogRepository(db_session),
    )
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
        reconciler=reconciler,
    )
    await kernel.start()
    yield kernel
    await kernel.stop()
    await db_session.rollback()


@pytest.mark.integration
async def test_close_cancel_cancels_all_active(
    kernel_with_reconciler: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    """收盘撤单任务撤掉所有活动限价单。"""
    kernel = kernel_with_reconciler
    om = kernel.order_manager

    # 下 3 个限价单(远离市价不会成交)
    cids: list[str] = []
    for i in range(3):
        order = await om.place_order(
            OrderRequest(
                account_id=account_id,
                symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("100"),
                price=Decimal(f"3.{i:02d}"),
            )
        )
        cids.append(str(order.client_order_id))

    # 确认都在 ACKNOWLEDGED
    for cid in cids:
        await wait_for_status(
            kernel.order_manager._orders,
            cid,
            target=OrderStatus.ACKNOWLEDGED,
        )

    # 手动触发收盘撤单任务
    task = close_cancel_task(kernel=kernel, order_manager=om)
    await task.func()

    # 验证所有订单都被撤
    repo = kernel.order_manager._orders
    for cid in cids:
        db_order = await repo.get(cid)
        assert db_order is not None
        assert db_order.status in (
            OrderStatus.CANCELLED,
            OrderStatus.CANCEL_PENDING,
        )


@pytest.mark.integration
async def test_close_cancel_empty_is_noop(
    kernel_with_reconciler: TradingKernel,
) -> None:
    """无活动订单时收盘撤单正常返回。"""
    kernel = kernel_with_reconciler
    task = close_cancel_task(kernel=kernel, order_manager=kernel.order_manager)
    await task.func()  # should not raise


@pytest.mark.integration
async def test_eod_reconcile_ok_no_kill_switch(
    kernel_with_reconciler: TradingKernel,
    db_session,
    account_id: AccountId,
) -> None:
    """日终核对通过(无差异)不激活 Kill Switch。"""
    kernel = kernel_with_reconciler
    reconciler = ReconciliationEngine(
        broker=kernel.broker,
        account_id=account_id,
        order_repo=OrderRepository(db_session),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        log_repo=ReconciliationLogRepository(db_session),
    )
    task = end_of_day_reconcile_task(
        kernel=kernel,
        reconciler=reconciler,
        audit_repo=AuditLogRepository(db_session),
        account_id=account_id,
    )
    await task.func()
    assert kernel.kill_switch_level == KillSwitchLevel.OFF


@pytest.mark.integration
async def test_heartbeat_detects_disconnect(
    kernel_with_reconciler: TradingKernel,
    db_session,
) -> None:
    """心跳任务检测 broker 断连后激活 Kill Switch。"""
    kernel = kernel_with_reconciler

    # 手动断连 broker 模拟掉线
    await kernel.broker.disconnect()

    task = heartbeat_task(kernel=kernel, interval=1.0)
    await task.func()

    assert kernel.kill_switch_level == KillSwitchLevel.NO_NEW_ORDERS


@pytest.mark.integration
async def test_heartbeat_ok_when_connected(
    kernel_with_reconciler: TradingKernel,
) -> None:
    """broker 正常连接时心跳不激活 Kill Switch。"""
    kernel = kernel_with_reconciler
    task = heartbeat_task(kernel=kernel, interval=1.0)
    await task.func()
    assert kernel.kill_switch_level == KillSwitchLevel.OFF


@pytest.mark.integration
async def test_pre_market_check_not_connected(
    db_session,
    account_id: AccountId,
) -> None:
    """盘前检查发现 broker 未连接 → 激活 Kill Switch。"""
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

    # 断连 broker
    await broker.disconnect()

    task = pre_market_check_task(kernel=kernel)
    await task.func()

    assert kernel.kill_switch_level == KillSwitchLevel.NO_NEW_ORDERS
    await kernel.stop()
    await db_session.rollback()


@pytest.mark.integration
async def test_scheduler_trigger_builtin_task(
    kernel_with_reconciler: TradingKernel,
    db_session,
    account_id: AccountId,
) -> None:
    """Scheduler.trigger 手动触发内置任务(heartbeat)。"""
    kernel = kernel_with_reconciler
    reconciler = ReconciliationEngine(
        broker=kernel.broker,
        account_id=account_id,
        order_repo=OrderRepository(db_session),
        position_repo=PositionRepository(db_session),
        account_repo=AccountRepository(db_session),
        log_repo=ReconciliationLogRepository(db_session),
    )

    sched = Scheduler(TradingCalendar())
    for task in create_default_tasks(
        kernel=kernel,
        order_manager=kernel.order_manager,
        reconciler=reconciler,
        audit_repo=AuditLogRepository(db_session),
        account_id=account_id,
    ):
        sched.schedule(task)

    # trigger heartbeat — should not raise, Kill Switch stays OFF
    await sched.trigger("heartbeat")
    assert kernel.kill_switch_level == KillSwitchLevel.OFF


@pytest.mark.integration
async def test_scheduler_interval_execution_with_kernel(
    kernel_with_reconciler: TradingKernel,
    db_session,
    account_id: AccountId,
) -> None:
    """Scheduler interval 模式与 kernel 集成:多次心跳执行不崩溃。"""
    kernel = kernel_with_reconciler
    sched = Scheduler(TradingCalendar())
    sched.schedule(heartbeat_task(kernel=kernel, interval=0.05))

    await sched.start()
    await asyncio.sleep(0.25)
    await sched.stop()

    # broker 仍然连接,kill switch 不应被激活
    assert kernel.kill_switch_level == KillSwitchLevel.OFF


@pytest.mark.integration
async def test_cancel_all_active_via_order_manager(
    kernel_with_reconciler: TradingKernel,
    account_id: AccountId,
    wait_for_status,
) -> None:
    """直接测试 OrderManager.cancel_all_active() 方法。"""
    kernel = kernel_with_reconciler
    om = kernel.order_manager

    # 下 2 个限价单
    order1 = await om.place_order(
        OrderRequest(
            account_id=account_id,
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            price=Decimal("2.50"),
        )
    )
    order2 = await om.place_order(
        OrderRequest(
            account_id=account_id,
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("200"),
            price=Decimal("2.40"),
        )
    )

    cid1 = str(order1.client_order_id)
    cid2 = str(order2.client_order_id)

    # Wait for both to be ACKNOWLEDGED
    await wait_for_status(om._orders, cid1, OrderStatus.ACKNOWLEDGED)
    await wait_for_status(om._orders, cid2, OrderStatus.ACKNOWLEDGED)

    cancelled = await om.cancel_all_active()
    assert len(cancelled) == 2
    assert cid1 in cancelled
    assert cid2 in cancelled
