"""策略运行器端到端集成测试 —— MockBroker + 真实 PG。

验证完整链路:
  策略 on_timer → StrategyContext.submit_order → 风控 → OrderManager → MockBroker
  → 成交回报 → OrderFilled → PositionManager.apply_fill → 持仓更新
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

from finboard_broker import create_broker
from finboard_core import TradingKernel
from finboard_core.events import OrderFilled
from finboard_core.strategy import StrategyRunner
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)
from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId


@pytest_asyncio.fixture
async def kernel(
    db_session,
    account_id: AccountId,
) -> AsyncIterator[TradingKernel]:
    broker = create_broker("mock")
    cfg = RiskConfig(allow_market_order=True)
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
async def test_etf_dca_strategy_full_chain(
    kernel: TradingKernel, account_id: AccountId
) -> None:
    """EtfDca 策略: 定时器触发 → 下市价单 → 成交 → 持仓更新。"""
    from finboard_app.strategies.etf_dca import EtfDcaStrategy

    runner = StrategyRunner(
        event_bus=kernel.event_bus,
        order_manager=kernel.order_manager,
        position_manager=kernel.position_manager,
        account_manager=kernel.account_manager,
        account_id=account_id,
        timer_interval=0.1,
    )

    strategy = EtfDcaStrategy(
        strategy_id="dca-test",
        symbol_code="510300.SH",
        quantity=Decimal("100"),
    )
    runner.register(strategy)

    # 订阅 OrderFilled,用于等待成交完成
    filled = asyncio.Event()

    async def _on_filled(event: OrderFilled) -> None:
        filled.set()

    kernel.event_bus.subscribe(OrderFilled, _on_filled)

    await runner.start()
    try:
        await asyncio.wait_for(filled.wait(), timeout=5.0)

        # PositionManager.apply_fill 在 OrderFilled 的内核 handler 中调用,
        # 但 DB flush 可能需要 yield 一次
        await asyncio.sleep(0.05)

        positions = await kernel.position_manager.list_local()
        etf = [p for p in positions if p.symbol.code == "510300.SH"]
        assert len(etf) == 1
        assert etf[0].total_quantity == Decimal("100")
    finally:
        await runner.stop()


@pytest.mark.integration
async def test_periodic_query_strategy_no_crash(
    kernel: TradingKernel, account_id: AccountId
) -> None:
    """PeriodicQuery 策略正常查询不崩溃。"""
    from finboard_app.strategies.periodic_query import PeriodicQueryStrategy

    runner = StrategyRunner(
        event_bus=kernel.event_bus,
        order_manager=kernel.order_manager,
        position_manager=kernel.position_manager,
        account_manager=kernel.account_manager,
        account_id=account_id,
        timer_interval=0.1,
    )

    strategy = PeriodicQueryStrategy(strategy_id="query-test")
    runner.register(strategy)

    await runner.start()
    try:
        # 等几个 timer 周期
        await asyncio.sleep(0.35)
    finally:
        await runner.stop()


@pytest.mark.integration
async def test_strategy_order_event_routed(
    kernel: TradingKernel, account_id: AccountId
) -> None:
    """策略下单后 on_order_update 被正确路由(created + filled)。"""
    from finboard_app.strategies.etf_dca import EtfDcaStrategy
    from finboard_core.strategy import OrderEvent

    runner = StrategyRunner(
        event_bus=kernel.event_bus,
        order_manager=kernel.order_manager,
        position_manager=kernel.position_manager,
        account_manager=kernel.account_manager,
        account_id=account_id,
        timer_interval=0.1,
    )

    received: list[str] = []

    class TrackingStrategy(EtfDcaStrategy):
        async def on_order_update(
            self, event: OrderEvent, ctx
        ) -> None:
            received.append(event.kind)

    strategy = TrackingStrategy(
        strategy_id="tracking-test",
        symbol_code="159919.SZ",
        quantity=Decimal("200"),
    )
    runner.register(strategy)

    await runner.start()
    try:
        await asyncio.sleep(0.5)
        # 应收到 created + submitted + filled
        assert "created" in received
        assert "filled" in received
    finally:
        await runner.stop()
