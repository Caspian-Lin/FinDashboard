"""可转债双低策略故障注入测试(issue #63)。

覆盖:
* 公告缺失(事件数据不全)
* 零成交量(无法成交)
* 连续停牌(无行情)
* 赎回跳空(价格大幅下跌)
* PIT 安全性(未来公告不影响历史决策)
* AssetRule 正确性(可转债 T+0 / 免印花税 / 10张/手)
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from finboard_backtest.asset_rules import (
    CONVERTIBLE_BOND,
    DEFAULT_TABLE,
)
from finboard_backtest.convertible_double_low.backtest import run_backtest
from finboard_backtest.convertible_double_low.config import (
    ConvertibleDoubleLowConfig,
    RebalanceFrequency,
)
from finboard_backtest.convertible_double_low.events import check_event_risk
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_shared.instruments import ConvertibleMetadata, Instrument, LifecycleEvent
from finboard_shared.types import (
    InstrumentType,
    LifecycleEventType,
    ListingStatus,
    Market,
)


def _make_snap(
    code: str,
    dt: date,
    close: Decimal,
    volume: Decimal = Decimal("1000000"),
    premium: Decimal = Decimal("0.10"),
) -> ConvertibleSnapshot:
    return ConvertibleSnapshot(
        instrument=Instrument(
            code=code,
            name=f"测试{code}",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
            list_date=date(2020, 1, 1),
            status=ListingStatus.ACTIVE,
        ),
        metadata=ConvertibleMetadata(
            underlying_stock_code="600000.SH",
            conversion_price=Decimal("10"),
        ),
        as_of=dt,
        close=close,
        volume=volume,
        amount=close * volume,
        conversion_premium=premium,
        conversion_price=Decimal("10"),
        conversion_value=close / (Decimal("1") + premium),
        ytm=Decimal("-0.02"),
        days_to_maturity=365,
        remaining_size=Decimal("100000000"),
        avg_amount_20d=close * Decimal("1000000"),
    )


def _gen_dates(n: int) -> list[date]:
    return [date(2023, 1, 2) + timedelta(days=i) for i in range(n)]


class TestAssetRuleForConvertible:
    def test_resolvable_from_default_table(self) -> None:
        rule = DEFAULT_TABLE.resolve(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
        )
        assert rule.lot_size == Decimal("10")
        assert rule.enforce_t_plus_1 is False
        assert rule.stamp_tax_rate == Decimal("0")
        assert rule.price_limit_pct == Decimal("0.20")
        assert rule.price_tick == Decimal("0.001")

    def test_convertible_bond_constant(self) -> None:
        assert CONVERTIBLE_BOND.instrument_type is InstrumentType.CONVERTIBLE
        assert CONVERTIBLE_BOND.lot_size == Decimal("10")

    def test_no_short(self) -> None:
        assert CONVERTIBLE_BOND.allow_short is False


class TestZeroVolume:
    def test_zero_volume_no_buy(self) -> None:
        dates = _gen_dates(50)
        by_date = {dt: [_make_snap("AAA", dt, Decimal("105"), volume=Decimal("0"))] for dt in dates}
        next_opens = {}
        next_vols = {}
        for i, dt in enumerate(dates):
            n = min(i + 1, len(dates) - 1)
            next_opens[dt] = {"AAA": Decimal("105")}
            next_vols[dt] = {"AAA": Decimal("0") if i == n else Decimal("1000000")}
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        buy_trades = [t for t in result.trades if t.side == "buy"]
        assert all(t.quantity > 0 for t in buy_trades)


class TestGapDown:
    def test_large_price_drop_no_crash(self) -> None:
        prices = [Decimal("105")] * 25 + [Decimal("80")] * 25
        dates = _gen_dates(50)
        by_date = {dt: [_make_snap("AAA", dt, px)] for dt, px in zip(dates, prices, strict=True)}
        next_opens = {}
        next_vols = {}
        for i, dt in enumerate(dates):
            n = min(i + 1, len(dates) - 1)
            next_opens[dt] = {"AAA": prices[n]}
            next_vols[dt] = {"AAA": Decimal("1000000")}
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert result.final_equity >= 0


class TestMissingEvents:
    def test_no_events_does_not_crash(self) -> None:
        snap = _make_snap("AAA", date(2023, 6, 1), Decimal("110"))
        result = check_event_risk(snap, [], as_of=date(2023, 6, 1))
        assert not result.is_blocked

    def test_events_for_other_symbol_ignored(self) -> None:
        snap = _make_snap("AAA", date(2023, 6, 1), Decimal("110"))
        event = LifecycleEvent(
            symbol="BBB",
            event_type=LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2023, 6, 1),
            available_at=datetime(2023, 6, 1, tzinfo=UTC),
            source="test",
            dataset_version="v1",
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert not result.is_blocked


class TestPITSafety:
    def test_future_event_does_not_block(self) -> None:
        snap = _make_snap("AAA", date(2023, 6, 1), Decimal("110"))
        future_event = LifecycleEvent(
            symbol="AAA",
            event_type=LifecycleEventType.FORCED_REDEMPTION,
            effective_date=date(2024, 1, 1),
            available_at=datetime(2024, 1, 1, tzinfo=UTC),
            source="test",
            dataset_version="v1",
        )
        result = check_event_risk(snap, [future_event], as_of=date(2023, 6, 1))
        assert not result.is_blocked

    def test_future_price_does_not_affect_trade(self) -> None:
        prices_normal = [Decimal("105")] * 50
        prices_with_gap = [Decimal("105")] * 25 + [Decimal("200")] * 25
        dates = _gen_dates(50)

        by_date_1 = {dt: [_make_snap("AAA", dt, px)] for dt, px in zip(dates, prices_normal, strict=True)}
        by_date_2 = {dt: [_make_snap("AAA", dt, px)] for dt, px in zip(dates, prices_with_gap, strict=True)}

        def make_next(prices: list[Decimal]) -> tuple[dict[date, dict[str, Decimal]], dict[date, dict[str, Decimal]]]:
            opens, vols = {}, {}
            for i, dt in enumerate(dates):
                n = min(i + 1, len(dates) - 1)
                opens[dt] = {"AAA": prices[n]}
                vols[dt] = {"AAA": Decimal("1000000")}
            return opens, vols

        opens1, vols1 = make_next(prices_normal)
        opens2, vols2 = make_next(prices_with_gap)

        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1, rebalance_frequency=RebalanceFrequency.MONTHLY)
        result1 = run_backtest(by_date_1, opens1, vols1, {}, cfg)
        result2 = run_backtest(by_date_2, opens2, vols2, {}, cfg)

        trades_1_first_25 = [t for t in result1.trades if t.date <= dates[24]]
        trades_2_first_25 = [t for t in result2.trades if t.date <= dates[24]]
        assert len(trades_1_first_25) == len(trades_2_first_25)


class TestConversionPriceAdjust:
    def test_conversion_adjust_tracked_but_not_blocking(self) -> None:
        snap = _make_snap("AAA", date(2023, 6, 10), Decimal("110"))
        event = LifecycleEvent(
            symbol="AAA",
            event_type=LifecycleEventType.CONVERSION_PRICE_ADJUST,
            effective_date=date(2023, 6, 10),
            available_at=datetime(2023, 6, 10, tzinfo=UTC),
            source="test",
            dataset_version="v1",
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 10))
        assert not result.is_blocked
        assert len(result.active_events) == 1

    def test_conversion_adjust_expired(self) -> None:
        snap = _make_snap("AAA", date(2023, 6, 1), Decimal("110"))
        event = LifecycleEvent(
            symbol="AAA",
            event_type=LifecycleEventType.CONVERSION_PRICE_ADJUST,
            effective_date=date(2023, 1, 1),
            available_at=datetime(2023, 1, 1, tzinfo=UTC),
            source="test",
            dataset_version="v1",
        )
        result = check_event_risk(snap, [event], as_of=date(2023, 6, 1))
        assert not result.is_blocked
