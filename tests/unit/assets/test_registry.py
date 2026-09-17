"""InstrumentRegistry / fail-closed 解析的单元测试(issue #58)。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finboard_data.assets.registry import (
    InstrumentRegistry,
    InstrumentResolutionError,
    resolve_market_by_code,
)
from finboard_shared.instruments import Instrument
from finboard_shared.types import (
    AssetClass,
    InstrumentType,
    ListingStatus,
    Market,
)


class TestResolveMarketByCode:
    def test_a_share_sh(self) -> None:
        assert resolve_market_by_code("600519.SH") is Market.A_SHARE

    def test_a_share_sz(self) -> None:
        assert resolve_market_by_code("000001.SZ") is Market.A_SHARE

    def test_a_share_bj(self) -> None:
        assert resolve_market_by_code("832000.BJ") is Market.A_SHARE

    def test_future_cffex(self) -> None:
        assert resolve_market_by_code("IF2406.CFFEX") is Market.FUTURE

    def test_future_shfe(self) -> None:
        assert resolve_market_by_code("CU2406.SHFE") is Market.FUTURE

    def test_hk(self) -> None:
        assert resolve_market_by_code("0700.HK") is Market.HK

    def test_us(self) -> None:
        assert resolve_market_by_code("AAPL.US") is Market.US

    def test_unknown_suffix_raises(self) -> None:
        with pytest.raises(InstrumentResolutionError):
            resolve_market_by_code("FOO.BAR")

    def test_no_suffix_raises(self) -> None:
        with pytest.raises(InstrumentResolutionError):
            resolve_market_by_code("600519")


class TestInstrumentRegistry:
    def test_register_and_resolve(self) -> None:
        reg = InstrumentRegistry()
        inst = Instrument(
            code="600519.SH",
            name="贵州茅台",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        )
        reg.register(inst)
        assert reg.contains("600519.SH")
        assert reg.resolve("600519.SH") is inst

    def test_register_many(self) -> None:
        reg = InstrumentRegistry()
        insts = [
            Instrument(
                code=f"{code}",
                name=code,
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            )
            for code in ("600519.SH", "000001.SZ")
        ]
        reg.register_many(insts)
        assert len(reg) == 2

    def test_resolve_unknown_a_share_synthesizes_etf(self) -> None:
        """未注册但有 .SH 后缀 + 5 开头 → 合成 ETF。"""
        reg = InstrumentRegistry()
        inst = reg.resolve("510300.SH")
        assert inst.instrument_type is InstrumentType.ETF
        assert inst.market is Market.A_SHARE

    def test_resolve_unknown_a_share_synthesizes_convertible(self) -> None:
        reg = InstrumentRegistry()
        inst = reg.resolve("113001.SH")
        assert inst.instrument_type is InstrumentType.CONVERTIBLE

    def test_resolve_unknown_a_share_synthesizes_bond(self) -> None:
        reg = InstrumentRegistry()
        inst = reg.resolve("019547.SH")
        assert inst.instrument_type is InstrumentType.BOND

    def test_resolve_unknown_a_share_synthesizes_stock(self) -> None:
        reg = InstrumentRegistry()
        inst = reg.resolve("600519.SH")
        assert inst.instrument_type is InstrumentType.STOCK

    def test_resolve_future_synthesizes(self) -> None:
        reg = InstrumentRegistry()
        inst = reg.resolve("IF2406.CFFEX")
        assert inst.instrument_type is InstrumentType.FUTURES
        assert inst.market is Market.FUTURE

    def test_resolve_unknown_no_suffix_raises(self) -> None:
        reg = InstrumentRegistry()
        with pytest.raises(InstrumentResolutionError, match="无已知后缀"):
            reg.resolve("xxx")

    def test_resolve_unknown_with_unknown_suffix_raises(self) -> None:
        reg = InstrumentRegistry()
        with pytest.raises(InstrumentResolutionError, match="未注册"):
            reg.resolve("FOO.XXX")

    def test_contains_operator(self) -> None:
        reg = InstrumentRegistry()
        reg.register(
            Instrument(
                code="x.SH",
                name="x",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            )
        )
        assert "x.SH" in reg
        assert "y.SH" not in reg
        assert 123 not in reg  # non-str

    def test_filter_by_type(self) -> None:
        reg = InstrumentRegistry(
            [
                Instrument(
                    code="600519.SH",
                    name="x",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                ),
                Instrument(
                    code="510300.SH",
                    name="y",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.ETF,
                ),
            ]
        )
        stocks = reg.filter_by_type(InstrumentType.STOCK)
        assert len(stocks) == 1
        assert stocks[0].code == "600519.SH"

    def test_filter_by_market(self) -> None:
        reg = InstrumentRegistry(
            [
                Instrument(
                    code="600519.SH",
                    name="x",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                ),
                Instrument(
                    code="IF2406.CFFEX",
                    name="y",
                    market=Market.FUTURE,
                    instrument_type=InstrumentType.FUTURES,
                ),
            ]
        )
        futures = reg.filter_by_market(Market.FUTURE)
        assert len(futures) == 1

    def test_list_all(self) -> None:
        reg = InstrumentRegistry(
            [
                Instrument(
                    code=f"{i}.SH",
                    name=str(i),
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                )
                for i in range(3)
            ]
        )
        assert len(reg.list_all()) == 3

    def test_resolution_caches(self) -> None:
        """同一代码第二次 resolve 返回同一对象(缓存)。"""
        reg = InstrumentRegistry()
        first = reg.resolve("510300.SH")
        second = reg.resolve("510300.SH")
        assert first is second


class TestEndToEnd:
    def test_full_flow(self) -> None:
        """端到端:注册 → 解析 → 校验 → 覆盖。"""
        reg = InstrumentRegistry()
        insts = [
            Instrument(
                code="600519.SH",
                name="贵州茅台",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
                lot_size=Decimal("100"),
                list_date=date(2001, 8, 27),
                status=ListingStatus.ACTIVE,
                asset_class=AssetClass.EQUITY,
            ),
            Instrument(
                code="510300.SH",
                name="沪深300ETF",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.ETF,
                asset_class=AssetClass.EQUITY,
            ),
            Instrument(
                code="019547.SH",
                name="国债",
                market=Market.A_SHARE,
                instrument_type=InstrumentType.BOND,
                asset_class=AssetClass.FIXED_INCOME,
            ),
            Instrument(
                code="IF2406.CFFEX",
                name="IF2406",
                market=Market.FUTURE,
                instrument_type=InstrumentType.FUTURES,
                asset_class=AssetClass.DERIVATIVE,
            ),
        ]
        reg.register_many(insts)
        assert len(reg) == 4
        assert reg.resolve("019547.SH").asset_class is AssetClass.FIXED_INCOME
        assert reg.resolve("IF2406.CFFEX").asset_class is AssetClass.DERIVATIVE
