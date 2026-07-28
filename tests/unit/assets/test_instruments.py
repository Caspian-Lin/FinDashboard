"""资产元数据契约 / Instrument / EtfMetadata 等的单元测试(issue #58)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_shared.instruments import (
    ASSET_METADATA_VERSION,
    BondMetadata,
    ConvertibleMetadata,
    DatasetManifest,
    EtfMetadata,
    FuturesContract,
    Instrument,
    LifecycleEvent,
)
from finboard_shared.types import (
    AdjustmentMethod,
    AssetClass,
    CouponFrequency,
    DatasetQualityStatus,
    EtfCategory,
    InstrumentType,
    LifecycleEventType,
    ListingStatus,
    Market,
    RollMethod,
)


class TestInstrumentType:
    def test_new_types_exist(self) -> None:
        assert InstrumentType.BOND.value == "bond"
        assert InstrumentType.CONVERTIBLE.value == "convertible"

    def test_asset_class_enum(self) -> None:
        assert AssetClass.EQUITY.value == "equity"
        assert AssetClass.FIXED_INCOME.value == "fixed_income"
        assert AssetClass.CONVERTIBLE.value == "convertible"
        assert AssetClass.DERIVATIVE.value == "derivative"

    def test_etf_category_enum(self) -> None:
        assert EtfCategory.EQUITY.value == "equity"
        assert EtfCategory.CROSS_BORDER.value == "cross_border"
        assert EtfCategory.BOND.value == "bond"
        assert EtfCategory.MONEY_MARKET.value == "money_market"


class TestInstrument:
    def test_stock(self) -> None:
        inst = Instrument(
            code="600519.SH",
            name="贵州茅台",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            exchange="SSE",
            lot_size=Decimal("100"),
            price_tick=Decimal("0.01"),
            list_date=date(2001, 8, 27),
            status=ListingStatus.ACTIVE,
            asset_class=AssetClass.EQUITY,
        )
        assert inst.code == "600519.SH"
        assert inst.is_active
        assert inst.multiplier == Decimal("1")
        assert inst.was_listed_at(date(2010, 1, 1))
        assert not inst.was_listed_at(date(2000, 1, 1))

    def test_etf(self) -> None:
        inst = Instrument(
            code="510300.SH",
            name="沪深300ETF",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            exchange="SSE",
            lot_size=Decimal("100"),
            status=ListingStatus.ACTIVE,
            asset_class=AssetClass.EQUITY,
        )
        assert inst.instrument_type is InstrumentType.ETF

    def test_convertible(self) -> None:
        inst = Instrument(
            code="113001.SH",
            name="中信转债",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
            lot_size=Decimal("10"),
            status=ListingStatus.ACTIVE,
            asset_class=AssetClass.CONVERTIBLE,
        )
        assert inst.asset_class is AssetClass.CONVERTIBLE

    def test_bond(self) -> None:
        inst = Instrument(
            code="019547.SH",
            name="国债",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.BOND,
            lot_size=Decimal("10"),
            status=ListingStatus.ACTIVE,
            asset_class=AssetClass.FIXED_INCOME,
        )
        assert inst.instrument_type is InstrumentType.BOND

    def test_futures_contract(self) -> None:
        inst = Instrument(
            code="IF2406.CFFEX",
            name="沪深300期货2406",
            market=Market.FUTURE,
            instrument_type=InstrumentType.FUTURES,
            exchange="CFFEX",
            multiplier=Decimal("300"),
            lot_size=Decimal("1"),
            status=ListingStatus.ACTIVE,
            asset_class=AssetClass.DERIVATIVE,
        )
        assert inst.multiplier == Decimal("300")

    def test_empty_code_raises(self) -> None:
        with pytest.raises(ValueError, match="code"):
            Instrument(
                code="",
                name="x",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            )

    def test_negative_multiplier_raises(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            Instrument(
                code="x",
                name="x",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                multiplier=Decimal("-1"),
            )

    def test_zero_lot_size_raises(self) -> None:
        with pytest.raises(ValueError, match="lot_size"):
            Instrument(
                code="x",
                name="x",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                lot_size=Decimal("0"),
            )

    def test_invalid_dates_raises(self) -> None:
        with pytest.raises(ValueError, match="list_date"):
            Instrument(
                code="x",
                name="x",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                list_date=date(2024, 1, 2),
                delist_date=date(2024, 1, 1),
            )

    def test_was_listed_at_with_no_dates(self) -> None:
        inst = Instrument(
            code="x",
            name="x",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        )
        assert inst.was_listed_at(date(2024, 1, 1))

    def test_delisted(self) -> None:
        inst = Instrument(
            code="x",
            name="x",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            status=ListingStatus.DELISTED,
        )
        assert inst.is_delisted
        assert not inst.is_active


class TestEtfMetadata:
    def test_equity_etf(self) -> None:
        m = EtfMetadata(
            fund_code="510300",
            category=EtfCategory.EQUITY,
            underlying_index="000300.SH",
            management_fee_rate=Decimal("0.005"),
            allows_t_plus_0=False,
        )
        assert m.category is EtfCategory.EQUITY
        assert m.underlying_index == "000300.SH"
        assert not m.allows_t_plus_0

    def test_cross_border_etf_t0(self) -> None:
        m = EtfMetadata(
            fund_code="513100",
            category=EtfCategory.CROSS_BORDER,
            allows_t_plus_0=True,
        )
        assert m.allows_t_plus_0

    def test_money_market_etf(self) -> None:
        m = EtfMetadata(
            fund_code="511990",
            category=EtfCategory.MONEY_MARKET,
            allows_t_plus_0=True,
            management_fee_rate=Decimal("0"),
        )
        assert m.category is EtfCategory.MONEY_MARKET


class TestBondMetadata:
    def test_treasury(self) -> None:
        m = BondMetadata(
            coupon_rate=Decimal("0.027"),
            coupon_frequency=CouponFrequency.SEMI_ANNUAL,
            maturity_date=date(2034, 6, 15),
            credit_entity_type="treasury",
            duration_years=Decimal("8.5"),
            yield_to_maturity=Decimal("0.025"),
        )
        assert m.is_treasury
        assert m.coupon_frequency is CouponFrequency.SEMI_ANNUAL

    def test_corporate(self) -> None:
        m = BondMetadata(
            coupon_rate=Decimal("0.04"),
            credit_entity_type="corporate",
            credit_rating="AA+",
        )
        assert not m.is_treasury

    def test_zero_coupon(self) -> None:
        m = BondMetadata(
            coupon_rate=None,
            coupon_frequency=CouponFrequency.ZERO_COUPON,
        )
        assert m.coupon_frequency is CouponFrequency.ZERO_COUPON


class TestConvertibleMetadata:
    def test_basic(self) -> None:
        m = ConvertibleMetadata(
            underlying_stock_code="600519.SH",
            conversion_price=Decimal("1500"),
            conversion_ratio=Decimal("0.0667"),
            forced_redeem_trigger=Decimal("1.30"),
            put_back_trigger=Decimal("0.70"),
        )
        assert m.conversion_price == Decimal("1500")
        assert m.forced_redeem_trigger == Decimal("1.30")

    def test_zero_conversion_price_raises(self) -> None:
        with pytest.raises(ValueError, match="conversion_price"):
            ConvertibleMetadata(
                underlying_stock_code="x",
                conversion_price=Decimal("0"),
            )


class TestFuturesContract:
    def test_index_futures(self) -> None:
        c = FuturesContract(
            contract_code="IF2406.CFFEX",
            underlying_symbol="000300.SH",
            exchange="CFFEX",
            multiplier=Decimal("300"),
            margin_rate=Decimal("0.12"),
            price_limit_pct=Decimal("0.10"),
            price_tick=Decimal("0.2"),
            last_trade_date=date(2024, 6, 21),
            delivery_method="cash",
        )
        assert c.multiplier == Decimal("300")
        assert c.exchange == "CFFEX"

    def test_treasury_futures(self) -> None:
        c = FuturesContract(
            contract_code="T2406.CFFEX",
            underlying_symbol="10y_treasury",
            exchange="CFFEX",
            multiplier=Decimal("10000"),
            margin_rate=Decimal("0.02"),
            price_limit_pct=Decimal("0.02"),
            price_tick=Decimal("0.005"),
        )
        assert c.multiplier == Decimal("10000")

    def test_invalid_margin_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="margin_rate"):
            FuturesContract(
                contract_code="x",
                underlying_symbol="x",
                exchange="x",
                multiplier=Decimal("1"),
                margin_rate=Decimal("1.5"),
                price_limit_pct=Decimal("0.1"),
                price_tick=Decimal("0.01"),
            )

    def test_empty_code_raises(self) -> None:
        with pytest.raises(ValueError, match="contract_code"):
            FuturesContract(
                contract_code="",
                underlying_symbol="x",
                exchange="x",
                multiplier=Decimal("1"),
                margin_rate=Decimal("0.1"),
                price_limit_pct=Decimal("0.1"),
                price_tick=Decimal("0.01"),
            )


class TestLifecycleEvent:
    def test_dividend(self) -> None:
        e = LifecycleEvent(
            symbol="600519.SH",
            event_type=LifecycleEventType.DIVIDEND,
            effective_date=date(2024, 6, 19),
            available_at=datetime(2024, 6, 19, 9, 0, tzinfo=UTC),
            source="tushare",
            dataset_version="2024Q2",
            details={"per_share": "25.91"},
        )
        assert e.event_type is LifecycleEventType.DIVIDEND

    def test_future_leakage_raises(self) -> None:
        """available_at 早于 effective_date 当日开盘应该 raise。"""
        with pytest.raises(ValueError, match="未来信息泄漏"):
            LifecycleEvent(
                symbol="x",
                event_type=LifecycleEventType.DIVIDEND,
                effective_date=date(2024, 6, 19),
                available_at=datetime(2024, 6, 18, 23, 59, tzinfo=UTC),
                source="x",
                dataset_version="x",
            )

    def test_available_equals_effective_ok(self) -> None:
        LifecycleEvent(
            symbol="x",
            event_type=LifecycleEventType.ROLL,
            effective_date=date(2024, 6, 19),
            available_at=datetime(2024, 6, 19, 0, 0, tzinfo=UTC),
            source="x",
            dataset_version="x",
        )

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            LifecycleEvent(
                symbol="",
                event_type=LifecycleEventType.OTHER,
                effective_date=date(2024, 1, 1),
                available_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
                source="x",
                dataset_version="x",
            )


class TestDatasetManifest:
    def test_passed_is_usable(self) -> None:
        m = DatasetManifest(
            dataset_name="daily_bars",
            source="akshare",
            version="2024Q1",
            quality_status=DatasetQualityStatus.PASSED,
            checksum="abc123",
        )
        assert m.is_usable

    def test_failed_not_usable(self) -> None:
        m = DatasetManifest(
            dataset_name="daily_bars",
            source="akshare",
            version="2024Q1",
            quality_status=DatasetQualityStatus.FAILED,
        )
        assert not m.is_usable

    def test_empty_dataset_name_raises(self) -> None:
        with pytest.raises(ValueError, match="dataset_name"):
            DatasetManifest(dataset_name="", source="x", version="x")


class TestEnums:
    def test_roll_method(self) -> None:
        assert RollMethod.VOLUME.value == "volume"
        assert RollMethod.OPEN_INTEREST.value == "open_interest"

    def test_adjustment_method(self) -> None:
        assert AdjustmentMethod.NONE.value == "none"
        assert AdjustmentMethod.RATIO.value == "ratio"
        assert AdjustmentMethod.DIFFERENCE.value == "difference"

    def test_dataset_quality_status(self) -> None:
        assert DatasetQualityStatus.PASSED.value == "passed"
        assert DatasetQualityStatus.FAILED.value == "failed"

    def test_listing_status(self) -> None:
        assert ListingStatus.ACTIVE.value == "active"
        assert ListingStatus.DELISTED.value == "delisted"
        assert ListingStatus.SUSPENDED.value == "suspended"

    def test_lifecycle_event_types(self) -> None:
        assert LifecycleEventType.DIVIDEND.value == "dividend"
        assert LifecycleEventType.FORCED_REDEMPTION.value == "forced_redemption"
        assert LifecycleEventType.ROLL.value == "roll"

    def test_coupon_frequency(self) -> None:
        assert CouponFrequency.ANNUAL.value == "annual"
        assert CouponFrequency.ZERO_COUPON.value == "zero_coupon"


def test_asset_metadata_version() -> None:
    assert ASSET_METADATA_VERSION == "v1"
