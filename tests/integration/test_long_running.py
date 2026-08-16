"""长期运行测试(§9.3)。

模拟 5 个交易日的连续运行:
* 每个交易日: 启动 → 下单 → 撮合 → 等待成交 → 停止
* 每次重启后 RecoveryEngine + ReconciliationEngine 自动修复状态
* 第 3 天注入网络抖动(FaultInjectionBroker 延迟)
* 验证最终成交数 / 订单状态 / 审计日志一致

用独立 engine 跨 session commit 真实持久化(同 test_restart_recovery 模式)。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from finboard_broker import FaultInjectionBroker, MockBroker
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
from finboard_shared.identifiers import AccountId
from finboard_shared.models import OrderRequest, Symbol
from finboard_shared.types import Market, OrderStatus, OrderType, Side
from tests.integration.conftest import clean_tables

pytestmark = pytest.mark.integration

TRADING_DAYS = 5
SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


def _make_kernel(
    broker: FaultInjectionBroker,
    account_id: AccountId,
    session,
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
        recoverer=RecoveryEngine(
            broker=broker,
            account_id=account_id,
            order_repo=OrderRepository(session),
            audit_repo=AuditLogRepository(session),
        ),
        reconciler=ReconciliationEngine(
            broker=broker,
            account_id=account_id,
            order_repo=OrderRepository(session),
            position_repo=PositionRepository(session),
            account_repo=AccountRepository(session),
            log_repo=ReconciliationLogRepository(session),
        ),
    )


async def _wait_filled(repo: OrderRepository, cid: str, timeout_s: float = 3.0) -> None:
    """等待订单变为 FILLED 状态(确保成交事件链完整处理)。"""
    async with asyncio.timeout(timeout_s):
        while True:
            order = await repo.get(cid)
            if order is not None and order.status is OrderStatus.FILLED:
                break
            await asyncio.sleep(0.02)
    # 额外等待 PositionManager.apply_fill 回调完成
    await asyncio.sleep(0.15)


async def test_multi_day_simulation(account_id: AccountId) -> None:
    """模拟 5 个交易日: 下单 → 撮合 → 重启恢复 → 网络抖动 → 最终一致。"""
    from tests.integration.conftest import DB_URL, ensure_test_db

    mock = MockBroker()
    broker = FaultInjectionBroker(mock)

    await ensure_test_db(DB_URL)
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        await clean_tables(conn)

    smaker = session_factory(engine)
    total_filled = 0

    try:
        for day in range(1, TRADING_DAYS + 1):
            # 第 3 天注入网络抖动(延迟),第 4 天恢复
            if day == 3:
                broker.place_delay = 0.02
                broker.query_delay = 0.01
            elif day == 4:
                broker.place_delay = 0.0
                broker.query_delay = 0.0

            async with smaker() as session:
                kernel = _make_kernel(broker, account_id, session)
                await kernel.start()
                assert kernel.ready, f"第 {day} 天内核未就绪"

                order_repo = OrderRepository(session)

                # 每天下 2 单并全部成交
                for _ in range(2):
                    req = OrderRequest(
                        account_id=account_id,
                        symbol=SYMBOL,
                        side=Side.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=Decimal("100"),
                        price=Decimal("4.50"),
                    )
                    order = await kernel.order_manager.place_order(req)
                    await asyncio.sleep(0.02)
                    await mock.match_limit_order(
                        str(order.client_order_id), Decimal("4.50")
                    )
                    await _wait_filled(order_repo, str(order.client_order_id))
                    total_filled += 1

                await kernel.stop()
                await session.commit()

        # 最终验证
        async with smaker() as session:
            # 总成交数正确
            _fills, fill_count = await FillRepository(session).list_by_account(
                str(account_id)
            )
            assert fill_count == total_filled, (
                f"成交数不一致: expected {total_filled}, got {fill_count}"
            )

            # 所有订单都是 FILLED(无残留活动订单)
            active = await OrderRepository(session).list_active(str(account_id))
            assert len(active) == 0, f"存在未关闭的活动订单: {len(active)}"

            # 审计日志覆盖所有交易日
            logs = await AuditLogRepository(session).list_recent(limit=200)
            actions = {log.action for log in logs}
            assert "place_order" in actions, "审计日志缺少 place_order"
            assert "order_filled" in actions, "审计日志缺少 order_filled"
            assert "reconcile" in actions, "审计日志缺少 reconcile"

            await session.rollback()
    finally:
        async with engine.begin() as conn:
            await clean_tables(conn)
        await engine.dispose()
