"""可转债事件风险过滤测试(issue #63)。"""

from datetime import UTC, date, datetime
from decimal import Decimal

from finboard_backtest.convertible_double_low.events import (
    check_event_risk,
    filter_event_risk,
)
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_shared.instruments import ConvertibleMetadata, Instrument, LifecycleEvent
from finboard_shared.types import (
    InstrumentType,
    LifecycleEventType,
    ListingStatus,
    Market,
)


def _make_snapshot(code: str = "113001.SH") -> ConvertibleSnapshot:
    return ConvertibleSnapshot(
        instrument=Instrument(
            code=code,
            name="测试转债",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
            list_date=date(2020, 1, 1),
            status=ListingStatus.ACTIVE,
        ),
        metadata=ConvertibleMetadata(
            underlying_stock_code="600519.SH",
            conversion_price=Decimal("10"),
        ),
        as_of=date(2023, 6, 1),
        close=Decimal("110"),
        volume=Decimal("100000"),
        amount=Decimal("11000000"),
        conversion_premium=Decimal("0.15"),
        conversion_price=Decimal("10"),
        conversion_value=Decimal("95.65"),
        ytm=Decimal("-0.02"),
        days_to_maturity=365,
        remaining_size=Decimal("100000000"),
        avg_amount_20d=Decimal("10000000"),
    )


def _make_event(
    symbol: str,
    event_type: LifecycleEventType,
    effective_date: date,
    available_at: datetime | None = None,
) -> LifecycleEvent:
    if available_at is None:
        available_at = datetime.combine(effective_date, datetime.min.time(), tzinfo=UTC)
    return LifecycleEvent(
        symbol=symbol,
        event_type=event_type,
        effective_date=effective_date,
        available_at=available_at,
        source="test",
        dataset_version="v1",
    )


class TestForcedRedemption:
    def test_blocks_after_announcement(self) -> None:
        snap = _make_snapshot()
        event = _make_event(
            "113001.SH",
            LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2023, 6, 1),
            available_at=datetime(2023, 6, 1, tzinfo=UTC),
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert result.is_blocked
        assert "forced_redemption" in result.block_reason

    def test_future_announcement_not_blocked(self) -> None:
        snap = _make_snapshot()
        event = _make_event(
            "113001.SH",
            LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2023, 7, 1),
            available_at=datetime(2023, 7, 1, tzinfo=UTC),
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert not result.is_blocked

    def test_no_events_not_blocked(self) -> None:
        snap = _make_snapshot()
        result = check_event_risk(snap, [], as_of=date(2023, 6, 1))
        assert not result.is_blocked


class TestDelisting:
    def test_blocks_delisting(self) -> None:
        snap = _make_snapshot()
        event = _make_event(
            "113001.SH",
            LifecycleEventType.DELISTING,
            effective_date=date(2023, 6, 1),
            available_at=datetime(2023, 6, 1, tzinfo=UTC),
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert result.is_blocked
        assert "delisting" in result.block_reason


class TestSellBack:
    def test_active_sell_back_blocks(self) -> None:
        snap = _make_snapshot()
        event = _make_event(
            "113001.SH",
            LifecycleEventType.SELL_BACK,
            effective_date=date(2023, 6, 10),
            available_at=datetime(2023, 6, 10, tzinfo=UTC),
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 10))
        assert result.is_blocked
        assert "sell_back" in result.block_reason

    def test_expired_sell_back_not_blocked(self) -> None:
        snap = _make_snapshot()
        event = _make_event(
            "113001.SH",
            LifecycleEventType.SELL_BACK,
            effective_date=date(2023, 1, 1),
            available_at=datetime(2023, 1, 1, tzinfo=UTC),
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert not result.is_blocked


class TestNearMaturity:
    def test_near_maturity_blocks(self) -> None:
        snap = _make_snapshot()
        snap_near = ConvertibleSnapshot(
            instrument=snap.instrument,
            metadata=snap.metadata,
            as_of=date(2023, 6, 1),
            close=snap.close,
            volume=snap.volume,
            amount=snap.amount,
            conversion_premium=snap.conversion_premium,
            conversion_price=snap.conversion_price,
            conversion_value=snap.conversion_value,
            ytm=snap.ytm,
            days_to_maturity=10,
            remaining_size=snap.remaining_size,
            avg_amount_20d=snap.avg_amount_20d,
        )
        result = check_event_risk(snap_near, [], as_of=date(2023, 6, 1), min_days_to_maturity=30)
        assert result.is_blocked
        assert "near_maturity" in result.block_reason


class TestFilterEventRisk:
    def test_filters_blocked(self) -> None:
        safe = _make_snapshot("AAA")
        blocked = _make_snapshot("BBB")
        forced_event = _make_event(
            "BBB",
            LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2023, 6, 1),
            available_at=datetime(2023, 6, 1, tzinfo=UTC),
        )
        events = {"AAA": [], "BBB": [forced_event]}
        safe_list, results = filter_event_risk(
            [safe, blocked], events, as_of=date(2023, 6, 1)
        )
        assert len(safe_list) == 1
        assert safe_list[0].code == "AAA"
        assert len(results) == 2

    def test_pit_safety_future_events_ignored(self) -> None:
        snap = _make_snapshot("AAA")
        future_event = _make_event(
            "AAA",
            LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2024, 1, 1),
            available_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        safe_list, _ = filter_event_risk(
            [snap], {"AAA": [future_event]}, as_of=date(2023, 6, 1)
        )
        assert len(safe_list) == 1
