"""#83 PostgreSQL 持久化模拟交易、隔离、恢复与期货结算。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.strategy_spec import (
    build_strategy_template,
    strategy_spec_checksum,
)
from finboard_persistence import session_factory
from finboard_persistence.models import (
    FillModel,
    OrderModel,
    PositionModel,
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchStrategySpecModel,
    SimulationMarketEventModel,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    BarPeriod,
    InstrumentType,
    Market,
    PositionSide,
)
from finboard_simulation import (
    SimulationBarEvent,
    SimulationConfig,
    SimulationConflictError,
    SimulationDecision,
    SimulationMatchingConfig,
    SimulationRepository,
    SimulationRiskLimits,
    SimulationService,
    SimulationSessionStatus,
    SimulationSourceMode,
    SimulationTarget,
    SimulationTransitionError,
    stable_checksum,
)

pytestmark = pytest.mark.integration


async def _seed_research(
    session: AsyncSession,
    *,
    strategy_id: str,
    strategy_kind: str,
    run_id: str,
    release_id: str,
    decision_id: str,
    signal_trace_id: str,
) -> None:
    spec = build_strategy_template(
        strategy_kind,
        strategy_id=strategy_id,
        dataset_release_ids=(release_id,),
    )
    checksum = strategy_spec_checksum(spec)
    session.add(
        ResearchStrategySpecModel(
            strategy_id=strategy_id,
            version=1,
            schema_version="v1",
            name=spec.name,
            strategy_kind=strategy_kind,
            status="published",
            change_type="create",
            checksum=checksum,
            payload=spec.canonical_payload(),
            validation_errors=[],
            published_at=datetime.now(UTC),
        )
    )
    manifest: dict[str, object] = {
        "strategy_spec_checksum": checksum,
        "strategy_spec": spec.canonical_payload(),
        "dataset_releases": [
            {
                "artifact_id": release_id,
                "version": "v1",
                "checksum": "release-checksum",
            }
        ],
    }
    session.add(
        ResearchRunModel(
            run_id=run_id,
            idempotency_key=f"idem-{run_id}",
            replay_of_run_id=None,
            strategy_id=strategy_id,
            strategy_kind=strategy_kind,
            status="completed",
            schema_version="v2",
            manifest_checksum=stable_checksum(manifest),
            manifest=manifest,
            result={
                "final_equity": "105000",
                "accounting_invariants_passed": True,
            },
            result_checksum="result-checksum",
            requested_by="researcher",
            completed_at=datetime.now(UTC),
        )
    )
    # ResearchRunArtifact uses an explicit foreign key without an ORM
    # relationship, so persist the parent before adding its trace artifact.
    await session.flush()
    session.add(
        ResearchRunArtifactModel(
            run_id=run_id,
            artifact_id=f"{run_id}:signals",
            decision_id=decision_id,
            sequence=1,
            stage="signals",
            trace_id=signal_trace_id,
            parent_trace_ids=[],
            payload={"signals": [{"symbol": strategy_id}]},
            checksum="signal-checksum",
        )
    )
    await session.flush()


def _bar(
    symbol: str,
    market: Market,
    timestamp: datetime,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str,
) -> Bar:
    return Bar(
        symbol=Symbol(symbol, market),
        period=BarPeriod.D1,
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


async def test_persistent_cash_simulation_full_lifecycle_and_isolation(
    db_session: AsyncSession,
) -> None:
    run_id = "RR-simulation-cash"
    research_decision = "research-decision-cash"
    trace_id = "trace-simulation-cash"
    await _seed_research(
        db_session,
        strategy_id="sim_etf_rotation",
        strategy_kind="etf_rotation",
        run_id=run_id,
        release_id="release-simulation-cash",
        decision_id=research_decision,
        signal_trace_id=trace_id,
    )
    repo = SimulationRepository(db_session)
    service = SimulationService(repo)
    account = await service.create_account(
        name="ETF simulation",
        initial_cash=Decimal("100000"),
        actor="tester",
    )
    simulation_session = await service.create_session(
        account_id=account.simulation_account_id,
        strategy_id="sim_etf_rotation",
        strategy_version=1,
        validation_run_id=run_id,
        data_release_id="release-simulation-cash",
        source_mode=SimulationSourceMode.HISTORICAL_REPLAY,
        config=SimulationConfig(
            matching=SimulationMatchingConfig(max_participation=Decimal("0.1"))
        ),
        actor="tester",
    )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.RUNNING,
        actor="tester",
    )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.PAUSED,
        actor="tester",
    )

    day1 = datetime(2025, 1, 2, 7, tzinfo=UTC)
    with pytest.raises(SimulationTransitionError, match="running"):
        await service.process_bar(
            simulation_session.simulation_session_id,
            SimulationBarEvent(
                "paused-bar",
                _bar(
                    "510300.SH",
                    Market.A_SHARE,
                    day1,
                    open_="10",
                    high="10.2",
                    low="9.8",
                    close="10",
                    volume="10000",
                ),
            ),
        )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.RUNNING,
        actor="tester",
    )
    first = await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "cash-bar-1",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day1,
                open_="10",
                high="10.2",
                low="9.8",
                close="10",
                volume="10000",
            ),
        ),
    )
    assert first.fill_ids == ()

    rejected_decision = SimulationDecision(
        decision_id="simulation-decision-risk-rejected",
        source_run_id=run_id,
        source_decision_id=research_decision,
        targets=(
            SimulationTarget(
                symbol="510300.SH",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.ETF,
                asset_rule_key="equity_etf",
                position_side=PositionSide.LONG,
                target_quantity=Decimal("20000"),
                signal_trace_id=trace_id,
                reason="超限目标",
            ),
        ),
        actor="strategy-runner",
    )
    _, rejected_orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id,
        rejected_decision,
    )
    assert rejected_orders[0].status == "rejected"
    assert rejected_orders[0].reject_reason == "max_order_value"
    assert account.frozen_cash == 0

    decision = SimulationDecision(
        decision_id="simulation-decision-open",
        source_run_id=run_id,
        source_decision_id=research_decision,
        targets=(
            SimulationTarget(
                symbol="510300.SH",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.ETF,
                asset_rule_key="equity_etf",
                position_side=PositionSide.LONG,
                target_quantity=Decimal("1000"),
                signal_trace_id=trace_id,
                reason="轮动目标",
            ),
        ),
        actor="strategy-runner",
    )
    _, orders, duplicate = await service.submit_decision(
        simulation_session.simulation_session_id, decision
    )
    assert not duplicate
    assert len(orders) == 1
    order = orders[0]
    assert order.status == "acknowledged"
    assert order.simulation_order_id.startswith("SIM-O-")
    assert account.frozen_cash > 0
    await service.cancel_order(
        simulation_session.simulation_session_id,
        order.simulation_order_id,
        actor="tester",
    )
    assert order.status == "cancelled"
    assert account.frozen_cash == 0

    execution_decision = SimulationDecision(
        decision_id="simulation-decision-open-retry",
        source_run_id=run_id,
        source_decision_id=research_decision,
        targets=decision.targets,
        actor="strategy-runner",
    )
    _, execution_orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id,
        execution_decision,
    )
    order = execution_orders[0]
    assert order.status == "acknowledged"

    day2 = day1 + timedelta(days=1)
    partial = await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "cash-bar-2",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day2,
                open_="10.2",
                high="10.4",
                low="10.1",
                close="10.3",
                volume="5000",
            ),
        ),
    )
    assert len(partial.fill_ids) == 1
    persisted_order = await repo.get_order(order.simulation_order_id)
    assert persisted_order is not None
    assert persisted_order.status == "partially_filled"
    assert persisted_order.filled_quantity == Decimal("500")

    duplicate_bar = await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "cash-bar-2",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day2,
                open_="10.2",
                high="10.4",
                low="10.1",
                close="10.3",
                volume="5000",
            ),
        ),
    )
    assert duplicate_bar.duplicate
    assert len(await repo.list_fills(simulation_session.simulation_session_id)) == 1

    day3 = day2 + timedelta(days=1)
    completed = await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "cash-bar-3",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day3,
                open_="10.4",
                high="10.6",
                low="10.2",
                close="10.5",
                volume="5000",
            ),
        ),
    )
    assert len(completed.fill_ids) == 1
    position = await repo.get_position(account.simulation_account_id, "510300.SH", "long")
    assert position is not None
    assert position.total_quantity == Decimal("1000")
    assert position.available_quantity == Decimal("500")

    close_decision = SimulationDecision(
        decision_id="simulation-decision-close",
        source_run_id=run_id,
        source_decision_id=research_decision,
        targets=(
            SimulationTarget(
                symbol="510300.SH",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.ETF,
                asset_rule_key="equity_etf",
                position_side=PositionSide.LONG,
                target_quantity=Decimal("500"),
                signal_trace_id=trace_id,
                reason="降低目标仓位",
            ),
        ),
        actor="strategy-runner",
    )
    _, close_orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id, close_decision
    )
    assert close_orders[0].reserved_quantity == Decimal("500")
    assert position.frozen_quantity == Decimal("500")

    day4 = day3 + timedelta(days=1)
    await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "cash-bar-4",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day4,
                open_="10.6",
                high="10.8",
                low="10.4",
                close="10.7",
                volume="10000",
            ),
        ),
    )
    assert position.total_quantity == Decimal("500")
    assert position.realized_pnl > 0

    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.STOPPED,
        actor="tester",
    )
    evaluation = await service.evaluate_session(
        simulation_session.simulation_session_id,
        actor="tester",
        minimum_trading_days=2,
    )
    assert evaluation["promotion_status"] == "eligible"
    assert evaluation["automatic_live_promotion"] is False
    report = await service.build_report(simulation_session.simulation_session_id)
    assert report["simulation_is_not_return_proof"] is True
    assert report["reference_backtest_final_equity"] == "105000"

    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.ARCHIVED,
        actor="tester",
    )
    assert account.status == "archived"
    with pytest.raises(SimulationTransitionError, match="归档"):
        await service.transition_session(
            simulation_session.simulation_session_id,
            target=SimulationSessionStatus.RUNNING,
            actor="tester",
        )

    new_account, new_session = await service.reset_session(
        simulation_session.simulation_session_id, actor="tester"
    )
    assert new_account.simulation_account_id != account.simulation_account_id
    assert new_session.reset_of_session_id == simulation_session.simulation_session_id
    assert await repo.list_audit(simulation_session.simulation_session_id)

    for live_model in (OrderModel, FillModel, PositionModel):
        count = (
            await db_session.execute(select(func.count()).select_from(live_model))
        ).scalar_one()
        assert count == 0


async def test_restart_recovery_repairs_reservations_and_retries_interrupted_event(
    db_session: AsyncSession,
) -> None:
    run_id = "RR-simulation-recovery"
    research_decision = "research-decision-recovery"
    trace_id = "trace-simulation-recovery"
    await _seed_research(
        db_session,
        strategy_id="sim_recovery_strategy",
        strategy_kind="etf_rotation",
        run_id=run_id,
        release_id="release-simulation-recovery",
        decision_id=research_decision,
        signal_trace_id=trace_id,
    )
    repo = SimulationRepository(db_session)
    service = SimulationService(repo)
    account = await service.create_account(
        name="recovery", initial_cash=Decimal("100000"), actor="tester"
    )
    simulation_session = await service.create_session(
        account_id=account.simulation_account_id,
        strategy_id="sim_recovery_strategy",
        strategy_version=1,
        validation_run_id=run_id,
        data_release_id="release-simulation-recovery",
        source_mode=SimulationSourceMode.READONLY_MARKET,
        config=SimulationConfig(),
        actor="tester",
    )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.RUNNING,
        actor="tester",
    )
    now = datetime(2025, 2, 3, 7, tzinfo=UTC)
    await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "recover-bar-1",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                now,
                open_="10",
                high="10.2",
                low="9.8",
                close="10",
                volume="10000",
            ),
        ),
    )
    _, orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id,
        SimulationDecision(
            decision_id="recover-decision",
            source_run_id=run_id,
            source_decision_id=research_decision,
            targets=(
                SimulationTarget(
                    symbol="510300.SH",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.ETF,
                    asset_rule_key="equity_etf",
                    position_side=PositionSide.LONG,
                    target_quantity=Decimal("100"),
                    signal_trace_id=trace_id,
                    reason="恢复测试",
                ),
            ),
            actor="strategy-runner",
        ),
    )
    reserved = orders[0].reserved_cash
    liquid = account.cash + account.frozen_cash
    account.cash = liquid
    account.frozen_cash = Decimal("0")
    interrupted_payload = SimulationBarEvent(
        "recover-bar-2",
        _bar(
            "510300.SH",
            Market.A_SHARE,
            now + timedelta(days=1),
            open_="10.1",
            high="10.3",
            low="10",
            close="10.2",
            volume="10000",
        ),
    )
    db_session.add(
        SimulationMarketEventModel(
            simulation_session_id=simulation_session.simulation_session_id,
            source_event_id=interrupted_payload.source_event_id,
            checksum=interrupted_payload.checksum,
            status="processing",
            symbol="510300.SH",
            market="a_share",
            timestamp=interrupted_payload.bar.timestamp,
            payload=interrupted_payload.as_dict(),
        )
    )
    await db_session.commit()
    db_session.expire_all()

    recovered = await SimulationService(SimulationRepository(db_session)).recover_active_sessions()
    await db_session.commit()
    assert recovered == (simulation_session.simulation_session_id,)
    recovered_account = await repo.get_account(account.simulation_account_id)
    assert recovered_account is not None
    assert recovered_account.frozen_cash == reserved
    interrupted = await repo.get_market_event(
        simulation_session.simulation_session_id, "recover-bar-2"
    )
    assert interrupted is not None
    assert interrupted.status == "failed"

    retried = await SimulationService(repo).process_bar(
        simulation_session.simulation_session_id, interrupted_payload
    )
    assert not retried.duplicate
    assert len(retried.fill_ids) == 1


async def test_futures_margin_daily_settlement_and_short_boundary(
    db_session: AsyncSession,
) -> None:
    run_id = "RR-simulation-futures"
    research_decision = "research-decision-futures"
    trace_id = "trace-simulation-futures"
    await _seed_research(
        db_session,
        strategy_id="sim_futures_tsmom",
        strategy_kind="futures_tsmom",
        run_id=run_id,
        release_id="release-simulation-futures",
        decision_id=research_decision,
        signal_trace_id=trace_id,
    )
    repo = SimulationRepository(db_session)
    service = SimulationService(repo)
    account = await service.create_account(
        name="futures", initial_cash=Decimal("500000"), actor="tester"
    )
    simulation_session = await service.create_session(
        account_id=account.simulation_account_id,
        strategy_id="sim_futures_tsmom",
        strategy_version=1,
        validation_run_id=run_id,
        data_release_id="release-simulation-futures",
        source_mode=SimulationSourceMode.HISTORICAL_REPLAY,
        config=SimulationConfig(
            risk=SimulationRiskLimits(
                max_order_value=Decimal("5000000"),
                max_symbol_position_value=Decimal("5000000"),
                max_gross_exposure=Decimal("4"),
                max_margin_usage=Decimal("0.8"),
                allow_short=True,
            )
        ),
        actor="tester",
    )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.RUNNING,
        actor="tester",
    )
    day1 = datetime(2025, 3, 3, 7, tzinfo=UTC)
    await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "future-bar-1",
            _bar(
                "IF2509.CFFEX",
                Market.FUTURE,
                day1,
                open_="4000",
                high="4010",
                low="3990",
                close="4000",
                volume="10000",
            ),
            contract_id="IF2509",
        ),
    )
    _, orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id,
        SimulationDecision(
            decision_id="future-short-open",
            source_run_id=run_id,
            source_decision_id=research_decision,
            targets=(
                SimulationTarget(
                    symbol="IF2509.CFFEX",
                    market=Market.FUTURE,
                    instrument_type=InstrumentType.FUTURES,
                    asset_rule_key="futures",
                    position_side=PositionSide.SHORT,
                    target_quantity=Decimal("1"),
                    signal_trace_id=trace_id,
                    reason="TSMOM 空头",
                ),
            ),
            actor="strategy-runner",
        ),
    )
    assert orders[0].side == "sell"
    assert orders[0].position_effect == "open"

    day2 = day1 + timedelta(days=1)
    result = await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "future-bar-2",
            _bar(
                "IF2509.CFFEX",
                Market.FUTURE,
                day2,
                open_="3990",
                high="4000",
                low="3970",
                close="3980",
                volume="10000",
            ),
            contract_id="IF2509",
        ),
    )
    assert result.fill_ids
    position = await repo.get_position(account.simulation_account_id, "IF2509.CFFEX", "short")
    assert position is not None
    assert position.total_quantity == Decimal("1")
    assert position.margin_used > 0
    assert position.last_settlement_price == Decimal("3980")
    equity_after_first_settlement = account.equity

    day3 = day2 + timedelta(days=1)
    await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "future-bar-3",
            _bar(
                "IF2509.CFFEX",
                Market.FUTURE,
                day3,
                open_="3975",
                high="3980",
                low="3950",
                close="3960",
                volume="10000",
            ),
            contract_id="IF2509",
        ),
    )
    assert account.equity > equity_after_first_settlement
    settlement_entries = [
        item
        for item in await repo.list_ledger(simulation_session.simulation_session_id)
        if item.event_type == "daily_settlement"
    ]
    assert len(settlement_entries) >= 2


async def test_transaction_failure_rolls_back_fill_and_original_event_retries(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "RR-simulation-transaction"
    decision_id = "research-decision-transaction"
    trace_id = "trace-simulation-transaction"
    await _seed_research(
        db_session,
        strategy_id="sim_transaction",
        strategy_kind="etf_rotation",
        run_id=run_id,
        release_id="release-simulation-transaction",
        decision_id=decision_id,
        signal_trace_id=trace_id,
    )
    repo = SimulationRepository(db_session)
    service = SimulationService(repo)
    account = await service.create_account(
        name="transaction failure simulation",
        initial_cash=Decimal("100000"),
        actor="tester",
    )
    simulation_session = await service.create_session(
        account_id=account.simulation_account_id,
        strategy_id="sim_transaction",
        strategy_version=1,
        validation_run_id=run_id,
        data_release_id="release-simulation-transaction",
        source_mode=SimulationSourceMode.READONLY_MARKET,
        config=SimulationConfig(),
        actor="tester",
    )
    await service.transition_session(
        simulation_session.simulation_session_id,
        target=SimulationSessionStatus.RUNNING,
        actor="tester",
    )
    day1 = datetime(2025, 4, 1, 7, tzinfo=UTC)
    await service.process_bar(
        simulation_session.simulation_session_id,
        SimulationBarEvent(
            "transaction-bar-1",
            _bar(
                "510300.SH",
                Market.A_SHARE,
                day1,
                open_="10",
                high="10.2",
                low="9.8",
                close="10",
                volume="10000",
            ),
        ),
    )
    _, orders, _ = await service.submit_decision(
        simulation_session.simulation_session_id,
        SimulationDecision(
            decision_id="transaction-open",
            source_run_id=run_id,
            source_decision_id=decision_id,
            targets=(
                SimulationTarget(
                    symbol="510300.SH",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.ETF,
                    asset_rule_key="equity_etf",
                    position_side=PositionSide.LONG,
                    target_quantity=Decimal("100"),
                    signal_trace_id=trace_id,
                    reason="事务故障注入",
                ),
            ),
            actor="strategy-runner",
        ),
    )
    order_id = orders[0].simulation_order_id
    session_id = simulation_session.simulation_session_id
    account_id = account.simulation_account_id
    await db_session.commit()

    original_add_fill = repo.add_fill

    async def fail_add_fill(**values: object) -> NoReturn:
        del values
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(repo, "add_fill", fail_add_fill)
    day2_event = SimulationBarEvent(
        "transaction-bar-2",
        _bar(
            "510300.SH",
            Market.A_SHARE,
            day1 + timedelta(days=1),
            open_="10.1",
            high="10.3",
            low="10",
            close="10.2",
            volume="10000",
        ),
    )
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await service.process_bar(session_id, day2_event)
    await db_session.rollback()

    persisted_order = await repo.get_order(order_id)
    assert persisted_order is not None
    assert persisted_order.status == "acknowledged"
    assert persisted_order.filled_quantity == 0
    assert await repo.get_market_event(session_id, day2_event.source_event_id) is None
    assert await repo.get_position(account_id, "510300.SH", "long") is None
    assert await repo.list_fills(session_id) == []

    monkeypatch.setattr(repo, "add_fill", original_add_fill)
    retried = await service.process_bar(session_id, day2_event)
    assert len(retried.fill_ids) == 1
    await db_session.commit()

    out_of_order = SimulationBarEvent(
        "transaction-out-of-order",
        _bar(
            "510300.SH",
            Market.A_SHARE,
            day1 + timedelta(hours=12),
            open_="10",
            high="10.1",
            low="9.9",
            close="10",
            volume="10000",
        ),
    )
    with pytest.raises(SimulationConflictError, match="时钟禁止倒退"):
        await service.process_bar(session_id, out_of_order)
    await db_session.rollback()
    assert len(await repo.list_fills(session_id)) == 1
    assert await repo.get_market_event(session_id, out_of_order.source_event_id) is None


async def test_concurrent_session_creation_and_account_isolation(
    db_session: AsyncSession,
    request: pytest.FixtureRequest,
) -> None:
    engine = cast(AsyncEngine, request.getfixturevalue("_engine"))
    run_id = "RR-simulation-concurrency"
    await _seed_research(
        db_session,
        strategy_id="sim_concurrency",
        strategy_kind="etf_rotation",
        run_id=run_id,
        release_id="release-simulation-concurrency",
        decision_id="research-decision-concurrency",
        signal_trace_id="trace-simulation-concurrency",
    )
    setup_repo = SimulationRepository(db_session)
    setup_service = SimulationService(setup_repo)
    account = await setup_service.create_account(
        name="concurrent account",
        initial_cash=Decimal("100000"),
        actor="tester",
    )
    account_id = account.simulation_account_id
    await db_session.commit()

    async def create_one(actor: str) -> str:
        maker = session_factory(engine)
        async with maker() as concurrent_session:
            concurrent_service = SimulationService(SimulationRepository(concurrent_session))
            try:
                created = await concurrent_service.create_session(
                    account_id=account_id,
                    strategy_id="sim_concurrency",
                    strategy_version=1,
                    validation_run_id=run_id,
                    data_release_id="release-simulation-concurrency",
                    source_mode=SimulationSourceMode.READONLY_MARKET,
                    config=SimulationConfig(),
                    actor=actor,
                )
                await concurrent_session.commit()
                return created.simulation_session_id
            except Exception:
                await concurrent_session.rollback()
                raise

    results = await asyncio.gather(
        create_one("concurrent-a"),
        create_one("concurrent-b"),
        return_exceptions=True,
    )
    successful = [item for item in results if isinstance(item, str)]
    conflicts = [item for item in results if isinstance(item, SimulationConflictError)]
    assert len(successful) == 1
    assert len(conflicts) == 1

    sessions = await setup_repo.list_sessions(account_id=account_id)
    assert len(sessions) == 1
    assert sessions[0].source_mode == "readonly_market"

    other_account = await setup_service.create_account(
        name="isolated account",
        initial_cash=Decimal("200000"),
        actor="tester",
    )
    other_session = await setup_service.create_session(
        account_id=other_account.simulation_account_id,
        strategy_id="sim_concurrency",
        strategy_version=1,
        validation_run_id=run_id,
        data_release_id="release-simulation-concurrency",
        source_mode=SimulationSourceMode.HISTORICAL_REPLAY,
        config=SimulationConfig(),
        actor="tester",
    )
    assert other_session.simulation_account_id != account_id
    assert len(await setup_repo.list_sessions(account_id=other_account.simulation_account_id)) == 1
