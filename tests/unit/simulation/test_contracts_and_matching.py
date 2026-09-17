"""#83 模拟域契约、资产规则与撮合纯函数。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.config import FillTiming
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    BarPeriod,
    InstrumentType,
    Market,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from finboard_simulation import (
    MatchAction,
    PendingOrder,
    SimulationBarEvent,
    SimulationConfig,
    SimulationDecision,
    SimulationMatchingConfig,
    SimulationRiskLimits,
    SimulationTarget,
    match_order,
    resolve_simulation_rule,
    simulation_rule_keys,
)


def _bar(
    timestamp: datetime,
    *,
    open_: str = "10",
    high: str = "10.5",
    low: str = "9.5",
    close: str = "10.2",
    volume: str = "1000",
) -> Bar:
    return Bar(
        symbol=Symbol("510300.SH", Market.A_SHARE),
        period=BarPeriod.D1,
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def test_simulation_contracts_are_targets_not_orders() -> None:
    target = SimulationTarget(
        symbol="510300.SH",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        asset_rule_key="equity_etf",
        position_side=PositionSide.LONG,
        target_quantity=Decimal("1000"),
        signal_trace_id="trace-signals",
        reason="已发布策略目标仓位",
    )
    decision = SimulationDecision(
        decision_id="decision-1",
        source_run_id="RR-validated",
        source_decision_id="research-decision-1",
        targets=(target,),
        actor="strategy-runner",
    )

    assert decision.checksum == decision.checksum
    assert target.as_dict()["target_quantity"] == "1000"
    assert all(
        forbidden not in target.as_dict()
        for forbidden in ("client_order_id", "broker", "python", "module")
    )


def test_config_forces_next_bar_and_valid_reservation_buffer() -> None:
    with pytest.raises(ValueError, match="next-bar"):
        SimulationMatchingConfig(next_bar_only=False)
    with pytest.raises(ValueError, match="缓冲"):
        SimulationMatchingConfig(
            slippage_bps=Decimal("20"),
            market_price_buffer_bps=Decimal("10"),
        )
    with pytest.raises(ValueError, match="保证金"):
        SimulationRiskLimits(max_margin_usage=Decimal("1.1"))
    assert SimulationConfig().matching.fill_timing is FillTiming.NEXT_BAR_OPEN


def test_bar_event_is_timezone_safe_and_content_addressed() -> None:
    now = datetime(2025, 1, 2, tzinfo=UTC)
    event = SimulationBarEvent("bar-1", _bar(now))
    assert event.checksum == event.checksum
    with pytest.raises(ValueError, match="时区"):
        SimulationBarEvent("bad", _bar(datetime(2025, 1, 2)))


def test_futures_event_requires_explicit_contract_for_auditable_roll() -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=UTC)
    future_bar = Bar(
        symbol=Symbol("IF2509.CFFEX", Market.FUTURE),
        period=BarPeriod.D1,
        timestamp=timestamp,
        open=Decimal("4000"),
        high=Decimal("4010"),
        low=Decimal("3990"),
        close=Decimal("4005"),
        volume=Decimal("1000"),
    )
    with pytest.raises(ValueError, match="contract_id"):
        SimulationBarEvent("future-bad", future_bar)
    event = SimulationBarEvent("future-good", future_bar, contract_id="IF2509")
    assert event.as_dict()["contract_id"] == "IF2509"


def test_rule_registry_is_explicit_and_unknown_fails_closed() -> None:
    assert "equity_etf" in simulation_rule_keys()
    assert "futures" in simulation_rule_keys()
    rule = resolve_simulation_rule(
        key="futures",
        market=Market.FUTURE,
        instrument_type=InstrumentType.FUTURES,
        symbol="IF2509.CFFEX",
    )
    assert rule.multiplier == Decimal("300")
    assert rule.margin_rate == Decimal("0.12")
    with pytest.raises(RuntimeError, match="未注册"):
        resolve_simulation_rule(
            key="qmt",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            symbol="600000.SH",
        )


def test_matching_enforces_next_bar_partial_fill_and_slippage() -> None:
    submitted_at = datetime(2025, 1, 2, tzinfo=UTC)
    pending = PendingOrder(
        order_id="SIM-O-test",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GFD,
        remaining_quantity=Decimal("1000"),
        limit_price=None,
        eligible_after=submitted_at,
    )
    rule = resolve_simulation_rule(
        key="equity_etf",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        symbol="510300.SH",
    )
    config = SimulationMatchingConfig(
        max_participation=Decimal("0.1"),
        slippage_bps=Decimal("5"),
    )

    same_bar = match_order(
        order=pending,
        bar=_bar(submitted_at),
        previous_close=Decimal("10"),
        asset_rule=rule,
        config=config,
    )
    assert same_bar.action is MatchAction.WAIT
    assert same_bar.reason == "next_bar_gate"

    next_bar = match_order(
        order=pending,
        bar=_bar(submitted_at + timedelta(days=1)),
        previous_close=Decimal("10"),
        asset_rule=rule,
        config=config,
    )
    assert next_bar.action is MatchAction.FILL
    assert next_bar.quantity == Decimal("100")
    assert next_bar.fill_price == Decimal("10.005")


@pytest.mark.parametrize("time_in_force", [TimeInForce.IOC, TimeInForce.FOK])
def test_unfillable_immediate_orders_cancel(time_in_force: TimeInForce) -> None:
    submitted_at = datetime(2025, 1, 2, tzinfo=UTC)
    outcome = match_order(
        order=PendingOrder(
            order_id="SIM-O-limit",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=time_in_force,
            remaining_quantity=Decimal("100"),
            limit_price=Decimal("9"),
            eligible_after=submitted_at,
        ),
        bar=_bar(submitted_at + timedelta(days=1), low="9.5"),
        previous_close=Decimal("10"),
        asset_rule=resolve_simulation_rule(
            key="equity_etf",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            symbol="510300.SH",
        ),
        config=SimulationMatchingConfig(),
    )
    assert outcome.action is MatchAction.CANCEL


def test_suspension_rejects_without_fill() -> None:
    submitted_at = datetime(2025, 1, 2, tzinfo=UTC)
    outcome = match_order(
        order=PendingOrder(
            order_id="SIM-O-suspended",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GFD,
            remaining_quantity=Decimal("100"),
            limit_price=None,
            eligible_after=submitted_at,
        ),
        bar=_bar(
            submitted_at + timedelta(days=1),
            open_="10",
            high="10",
            low="10",
            close="10",
            volume="0",
        ),
        previous_close=Decimal("10"),
        asset_rule=resolve_simulation_rule(
            key="equity_etf",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            symbol="510300.SH",
        ),
        config=SimulationMatchingConfig(),
    )
    assert outcome.action is MatchAction.REJECT
    assert outcome.reason == "suspended"
