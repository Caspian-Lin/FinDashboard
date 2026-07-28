"""可转债候选池 PIT 过滤测试(issue #63)。"""

from datetime import date
from decimal import Decimal

from finboard_backtest.convertible_double_low.config import ConvertibleDoubleLowConfig
from finboard_backtest.convertible_double_low.universe import (
    ConvertibleSnapshot,
    filter_universe,
    is_tradable_on,
)
from finboard_shared.instruments import ConvertibleMetadata, Instrument
from finboard_shared.types import InstrumentType, ListingStatus, Market


def _make_instrument(
    code: str = "113001.SH",
    list_date: date | None = date(2020, 1, 1),
    delist_date: date | None = None,
    status: ListingStatus = ListingStatus.ACTIVE,
) -> Instrument:
    return Instrument(
        code=code,
        name="测试转债",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.CONVERTIBLE,
        list_date=list_date,
        delist_date=delist_date,
        status=status,
    )


def _make_metadata(conv_price: Decimal = Decimal("10")) -> ConvertibleMetadata:
    return ConvertibleMetadata(
        underlying_stock_code="600519.SH",
        conversion_price=conv_price,
        maturity_date=date(2026, 1, 1),
    )


def _make_snapshot(
    code: str = "113001.SH",
    close: Decimal = Decimal("110"),
    premium: Decimal = Decimal("0.15"),
    volume: Decimal = Decimal("100000"),
    amount: Decimal = Decimal("11000000"),
    avg_amount_20d: Decimal | None = None,
    days_to_maturity: int = 365,
    remaining_size: Decimal = Decimal("100000000"),
    list_date: date | None = date(2020, 1, 1),
    as_of: date = date(2023, 6, 1),
    delist_date: date | None = None,
    status: ListingStatus = ListingStatus.ACTIVE,
) -> ConvertibleSnapshot:
    return ConvertibleSnapshot(
        instrument=_make_instrument(code, list_date, delist_date, status),
        metadata=_make_metadata(),
        as_of=as_of,
        close=close,
        volume=volume,
        amount=amount,
        conversion_premium=premium,
        conversion_price=Decimal("10"),
        conversion_value=close / (Decimal("1") + premium),
        ytm=Decimal("-0.02"),
        days_to_maturity=days_to_maturity,
        remaining_size=remaining_size,
        avg_amount_20d=avg_amount_20d or amount,
    )


class TestIsTradable:
    def test_normal_bond_is_tradable(self) -> None:
        snap = _make_snapshot()
        cfg = ConvertibleDoubleLowConfig()
        assert is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_too_new_excluded(self) -> None:
        snap = _make_snapshot(list_date=date(2023, 5, 28))
        cfg = ConvertibleDoubleLowConfig(universe_min_listing_days=15)
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_near_maturity_excluded(self) -> None:
        snap = _make_snapshot(days_to_maturity=10)
        cfg = ConvertibleDoubleLowConfig(universe_min_remaining_days=30)
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_low_amount_excluded(self) -> None:
        snap = _make_snapshot(avg_amount_20d=Decimal("100000"))
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_small_remaining_size_excluded(self) -> None:
        snap = _make_snapshot(remaining_size=Decimal("1000000"))
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_price_above_max_excluded(self) -> None:
        snap = _make_snapshot(close=Decimal("150"))
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_price_below_min_excluded(self) -> None:
        snap = _make_snapshot(close=Decimal("95"))
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_high_premium_excluded(self) -> None:
        snap = _make_snapshot(premium=Decimal("0.60"))
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))

    def test_delisted_excluded(self) -> None:
        snap = _make_snapshot(status=ListingStatus.DELISTED)
        cfg = ConvertibleDoubleLowConfig()
        assert not is_tradable_on(snap, cfg, as_of=date(2023, 6, 1))


class TestFilterUniverse:
    def test_filters_correctly(self) -> None:
        good = _make_snapshot(code="AAA")
        bad_price = _make_snapshot(code="BBB", close=Decimal("150"))
        bad_amount = _make_snapshot(
            code="CCC", avg_amount_20d=Decimal("10000")
        )
        cfg = ConvertibleDoubleLowConfig()
        result = filter_universe([good, bad_price, bad_amount], cfg, as_of=date(2023, 6, 1))
        codes = {s.code for s in result}
        assert "AAA" in codes
        assert "BBB" not in codes
        assert "CCC" not in codes

    def test_empty_input(self) -> None:
        cfg = ConvertibleDoubleLowConfig()
        assert filter_universe([], cfg, as_of=date(2023, 6, 1)) == []


class TestDoubleLowValue:
    def test_classic_formula(self) -> None:
        snap = _make_snapshot(close=Decimal("110"), premium=Decimal("0.15"))
        assert snap.double_low_value == Decimal("110") + Decimal("0.15") * Decimal("100")

    def test_lower_is_better(self) -> None:
        low_dl = _make_snapshot(close=Decimal("101"), premium=Decimal("0.05"))
        high_dl = _make_snapshot(close=Decimal("125"), premium=Decimal("0.30"))
        assert low_dl.double_low_value < high_dl.double_low_value
