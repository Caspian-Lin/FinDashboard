"""ETF 候选池与时点化过滤测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.etf_rotation import (
    DEFAULT_ETF_UNIVERSE,
    EtfSector,
    EtfUniverse,
    EtfUniverseMember,
    UniverseFilterConfig,
)
from finboard_shared.types import AssetClass, EtfCategory


class TestEtfUniverseMember:
    def test_asset_class_equity(self) -> None:
        m = EtfUniverseMember(
            code="510300.SH", name="沪深300",
            etf_category=EtfCategory.INDEX,
            sector=EtfSector.BROAD_INDEX,
            listing_date=date(2012, 5, 28),
        )
        assert m.asset_class is AssetClass.EQUITY
        assert m.is_listed
        assert m.lot_size == 100

    def test_asset_class_bond(self) -> None:
        m = EtfUniverseMember(
            code="511010.SH", name="国债",
            etf_category=EtfCategory.BOND,
            sector=EtfSector.GOVERNMENT_BOND,
            listing_date=date(2013, 3, 5),
            lot_size=10,
        )
        assert m.asset_class is AssetClass.FIXED_INCOME
        assert m.lot_size == 10

    def test_asset_class_gold(self) -> None:
        m = EtfUniverseMember(
            code="518880.SH", name="黄金",
            etf_category=EtfCategory.COMMODITY,
            sector=EtfSector.GOLD,
            listing_date=date(2013, 7, 29),
        )
        assert m.asset_class is AssetClass.COMMODITY

    def test_is_tradable_on_before_listing(self) -> None:
        m = EtfUniverseMember(
            code="X.SH", name="X",
            etf_category=EtfCategory.INDEX,
            sector=EtfSector.BROAD_INDEX,
            listing_date=date(2020, 1, 1),
        )
        assert not m.is_tradable_on(date(2019, 12, 31))

    def test_is_tradable_on_after_listing(self) -> None:
        m = EtfUniverseMember(
            code="X.SH", name="X",
            etf_category=EtfCategory.INDEX,
            sector=EtfSector.BROAD_INDEX,
            listing_date=date(2020, 1, 1),
        )
        assert m.is_tradable_on(date(2020, 6, 1))

    def test_is_tradable_on_delisted(self) -> None:
        m = EtfUniverseMember(
            code="X.SH", name="X",
            etf_category=EtfCategory.INDEX,
            sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
            delisting_date=date(2020, 1, 1),
        )
        assert not m.is_tradable_on(date(2020, 6, 1))
        assert m.is_tradable_on(date(2019, 6, 1))

    def test_is_listed_delisted(self) -> None:
        m = EtfUniverseMember(
            code="X.SH", name="X",
            etf_category=EtfCategory.INDEX,
            sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
            delisting_date=date(2020, 1, 1),
        )
        assert not m.is_listed


class TestEtfUniverse:
    def test_duplicate_codes_rejected(self) -> None:
        m1 = EtfUniverseMember(
            code="X.SH", name="X",
            etf_category=EtfCategory.INDEX, sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
        )
        m2 = EtfUniverseMember(
            code="X.SH", name="Dup",
            etf_category=EtfCategory.INDEX, sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
        )
        with pytest.raises(ValueError, match="重复"):
            EtfUniverse(members=(m1, m2))

    def test_get_found(self) -> None:
        u = DEFAULT_ETF_UNIVERSE
        m = u.get("510300.SH")
        assert m is not None
        assert m.name == "沪深300ETF"

    def test_get_not_found(self) -> None:
        assert DEFAULT_ETF_UNIVERSE.get("999999.SH") is None

    def test_codes(self) -> None:
        codes = DEFAULT_ETF_UNIVERSE.codes
        assert "510300.SH" in codes
        assert "511010.SH" in codes
        assert len(codes) == len(set(codes))


class TestUniverseFilterPit:
    def test_filter_excludes_not_yet_listed(self) -> None:
        as_of = date(2012, 1, 1)
        result = DEFAULT_ETF_UNIVERSE.filter_pit(as_of)
        codes = [m.code for m in result]
        # 科创50 listed 2020-09 should not appear
        assert "588000.SH" not in codes
        # 上证50 listed 2004 should appear
        assert "510050.SH" in codes

    def test_filter_min_listing_days(self) -> None:
        as_of = date(2012, 6, 28)
        # 沪深300 listed 2012-05-28, only 31 days old
        cfg = UniverseFilterConfig(min_listing_days=60)
        result = DEFAULT_ETF_UNIVERSE.filter_pit(as_of, cfg)
        codes = [m.code for m in result]
        assert "510300.SH" not in codes  # only 31 days
        assert "510050.SH" in codes  # listed 2004

    def test_filter_exclude_sectors(self) -> None:
        as_of = date(2024, 1, 1)
        cfg = UniverseFilterConfig(
            exclude_sectors=frozenset({EtfSector.MONEY_MARKET}),
        )
        result = DEFAULT_ETF_UNIVERSE.filter_pit(as_of, cfg)
        codes = [m.code for m in result]
        assert "511990.SH" not in codes  # 货币 ETF excluded
        assert "510300.SH" in codes

    def test_filter_delisted(self) -> None:
        m_active = EtfUniverseMember(
            code="A.SH", name="A",
            etf_category=EtfCategory.INDEX, sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
        )
        m_delisted = EtfUniverseMember(
            code="B.SH", name="B",
            etf_category=EtfCategory.INDEX, sector=EtfSector.BROAD_INDEX,
            listing_date=date(2010, 1, 1),
            delisting_date=date(2020, 1, 1),
        )
        u = EtfUniverse(members=(m_active, m_delisted))
        result = u.filter_pit(date(2021, 1, 1))
        codes = [m.code for m in result]
        assert "A.SH" in codes
        assert "B.SH" not in codes

    def test_filter_result_sorted(self) -> None:
        as_of = date(2024, 1, 1)
        result = DEFAULT_ETF_UNIVERSE.filter_pit(as_of)
        codes = [m.code for m in result]
        assert codes == sorted(codes)

    def test_default_universe_has_all_sectors(self) -> None:
        sectors = {m.sector for m in DEFAULT_ETF_UNIVERSE.members}
        assert EtfSector.BROAD_INDEX in sectors
        assert EtfSector.SECTOR in sectors
        assert EtfSector.DIVIDEND in sectors
        assert EtfSector.GOLD in sectors
        assert EtfSector.CROSS_BORDER in sectors
        assert EtfSector.GOVERNMENT_BOND in sectors

    def test_default_universe_size(self) -> None:
        assert len(DEFAULT_ETF_UNIVERSE.members) >= 15
