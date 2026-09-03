"""UniverseDiscovery + 代码归一化测试。"""

from __future__ import annotations

import pytest

from finboard_data.discovery import infer_a_share_listing_board, normalize_a_share_code
from finboard_shared.types import InstrumentType, ListingBoard, Market


class TestNormalizeCode:
    """代码归一化。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("000001", "000001.SZ"),  # 深交所主板
            ("000002", "000002.SZ"),
            ("300001", "300001.SZ"),  # 创业板
            ("600000", "600000.SH"),  # 上交所主板
            ("688001", "688001.SH"),  # 科创板
            ("920000", "920000.BJ"),  # 北交所新代码
            ("sh510300", "510300.SH"),
            ("sz159998", "159998.SZ"),
            ("510300.SH", "510300.SH"),  # 已归一化
            ("510300.SS", "510300.SS"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize_a_share_code(raw) == expected

    def test_normalize_invalid(self) -> None:
        assert normalize_a_share_code("ABCDEF") is None
        assert normalize_a_share_code("") is None
        assert normalize_a_share_code("12345") is None

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600000.SH", ListingBoard.SSE_MAIN),
            ("000001.SZ", ListingBoard.SZSE_MAIN),
            ("300001.SZ", ListingBoard.CHINEXT),
            ("688001.SH", ListingBoard.STAR),
            ("689001.SH", ListingBoard.CDR),
            ("001100.SZ", ListingBoard.CDR),
            ("309900.SZ", ListingBoard.CDR),
            ("920000.BJ", ListingBoard.BSE),
        ],
    )
    def test_infer_listing_board(self, code: str, expected: ListingBoard) -> None:
        assert infer_a_share_listing_board(code) is expected


class TestDiscoveryIntegration:
    """discovery 模块结构验证(不实际调用 akshare)。"""

    def test_instrument_info_fields(self) -> None:
        from finboard_data.discovery import InstrumentInfo

        info = InstrumentInfo(
            code="510300.SH",
            name="沪深300ETF",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            exchange="SSE",
        )
        assert info.code == "510300.SH"
        assert info.instrument_type is InstrumentType.ETF
        assert info.listing_board is ListingBoard.UNKNOWN

    def test_universe_discovery_class_exists(self) -> None:
        from finboard_data.discovery import UniverseDiscovery

        d = UniverseDiscovery()
        assert hasattr(d, "discover_a_shares")
        assert hasattr(d, "discover_a_etfs")
        assert hasattr(d, "discover_indices")
        assert hasattr(d, "discover_all")


class TestBenchmarkIndexRegistry:
    """基准指数受控登记表(issue #256)。"""

    def test_all_entries_satisfy_index_code_rule(self) -> None:
        """登记表全部条目满足 is_index_code 代码规则(模块导入期已断言)。"""
        from finboard_data.akshare_provider import is_index_code
        from finboard_data.discovery import BENCHMARK_INDEX_REGISTRY

        assert BENCHMARK_INDEX_REGISTRY
        for code, name in BENCHMARK_INDEX_REGISTRY:
            assert is_index_code(code), code
            assert name
            assert code == code.upper()

    def test_core_benchmark_indices_present(self) -> None:
        """研究基准常用指数必须在登记表中(沪深300/中证500/创业板指等)。"""
        from finboard_data.discovery import BENCHMARK_INDEX_REGISTRY

        codes = {code for code, _ in BENCHMARK_INDEX_REGISTRY}
        assert {
            "000001.SH",
            "000300.SH",
            "000905.SH",
            "000852.SH",
            "399001.SZ",
            "399006.SZ",
        } <= codes

    def test_registry_codes_unique(self) -> None:
        from finboard_data.discovery import BENCHMARK_INDEX_REGISTRY

        codes = [code for code, _ in BENCHMARK_INDEX_REGISTRY]
        assert len(codes) == len(set(codes))


class TestDiscoverIndices:
    """discover_indices:登记表 → instrument_type=index(issue #256)。"""

    async def test_discover_indices_returns_index_instruments(self) -> None:
        from finboard_data.discovery import BENCHMARK_INDEX_REGISTRY, UniverseDiscovery

        instruments = await UniverseDiscovery().discover_indices()
        assert len(instruments) == len(BENCHMARK_INDEX_REGISTRY)
        by_code = {item.code: item for item in instruments}
        for code, name in BENCHMARK_INDEX_REGISTRY:
            info = by_code[code]
            assert info.name == name
            assert info.market is Market.A_SHARE
            assert info.instrument_type is InstrumentType.INDEX
            assert info.listing_board is ListingBoard.UNKNOWN
            expected_exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[
                code.rpartition(".")[2]
            ]
            assert info.exchange == expected_exchange

    async def test_discover_all_includes_indices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discover_all = 股票 + ETF + 指数;akshare 依赖被打桩(不联网)。"""
        from finboard_data import FUTURES_MAIN_SERIES_REGISTRY
        from finboard_data.discovery import (
            BENCHMARK_INDEX_REGISTRY,
            InstrumentInfo,
            UniverseDiscovery,
        )

        d = UniverseDiscovery()

        async def _fake_stocks() -> list[InstrumentInfo]:
            return [
                InstrumentInfo(
                    code="600000.SH",
                    name="浦发银行",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.STOCK,
                    exchange="SSE",
                )
            ]

        async def _fake_etfs() -> list[InstrumentInfo]:
            return [
                InstrumentInfo(
                    code="510300.SH",
                    name="沪深300ETF",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.ETF,
                    exchange="SSE",
                )
            ]

        async def _fake_convertibles() -> list[InstrumentInfo]:
            return [
                InstrumentInfo(
                    code="113050.SH",
                    name="南银转债",
                    market=Market.A_SHARE,
                    instrument_type=InstrumentType.CONVERTIBLE,
                    exchange="SSE",
                )
            ]

        monkeypatch.setattr(d, "discover_a_shares", _fake_stocks)
        monkeypatch.setattr(d, "discover_a_etfs", _fake_etfs)
        # #265:discover_all 并入转债段,同样打桩保持测试离线。
        monkeypatch.setattr(d, "discover_convertibles", _fake_convertibles)
        all_instruments = await d.discover_all()
        by_type = {item.instrument_type for item in all_instruments}
        # #267:discover_all 并入期货主连段(受控登记表,无网络)。
        assert by_type == {
            InstrumentType.STOCK,
            InstrumentType.ETF,
            InstrumentType.INDEX,
            InstrumentType.CONVERTIBLE,
            InstrumentType.FUTURES,
        }
        assert len(all_instruments) == (
            3 + len(BENCHMARK_INDEX_REGISTRY) + len(FUTURES_MAIN_SERIES_REGISTRY)
        )


# ---------------------------------------------------------------------------
# #265:可转债登记(discover_convertibles,东财一览)
# ---------------------------------------------------------------------------


class TestDiscoverConvertibles:
    async def test_discover_convertibles_normalizes_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """bond_zh_cov 一览 → convertible 标的;非转债段跳过;交易所按代码段。"""
        import pandas as pd

        from finboard_data.discovery import UniverseDiscovery

        frame = pd.DataFrame(
            {
                "债券代码": ["113050", "123101", "500001"],
                "债券简称": ["南银转债", "天赐转2", "已兑付归档"],
                "正股代码": ["601009", "002709", "600000"],
            }
        )
        monkeypatch.setattr(
            "akshare.bond_zh_cov", lambda: frame
        )

        instruments = await UniverseDiscovery().discover_convertibles()

        assert [(item.code, item.name) for item in instruments] == [
            ("113050.SH", "南银转债"),
            ("123101.SZ", "天赐转2"),
        ]
        for item in instruments:
            assert item.market is Market.A_SHARE
            assert item.instrument_type is InstrumentType.CONVERTIBLE
        assert instruments[0].exchange == "SSE"
        assert instruments[1].exchange == "SZSE"

    async def test_discover_all_includes_convertibles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discover_all 并入转债段(#265);akshare 依赖被打桩(不联网)。"""
        import pandas as pd

        from finboard_data import FUTURES_MAIN_SERIES_REGISTRY
        from finboard_data.discovery import (
            BENCHMARK_INDEX_REGISTRY,
            InstrumentInfo,
            UniverseDiscovery,
        )

        d = UniverseDiscovery()

        async def _empty() -> list[InstrumentInfo]:
            return []

        frame = pd.DataFrame(
            {
                "债券代码": ["113050"],
                "债券简称": ["南银转债"],
                "正股代码": ["601009"],
            }
        )
        monkeypatch.setattr(d, "discover_a_shares", _empty)
        monkeypatch.setattr(d, "discover_a_etfs", _empty)
        monkeypatch.setattr("akshare.bond_zh_cov", lambda: frame)

        all_instruments = await d.discover_all()
        by_type = {item.instrument_type for item in all_instruments}
        assert by_type == {
            InstrumentType.CONVERTIBLE,
            InstrumentType.INDEX,
            InstrumentType.FUTURES,
        }
        assert len(all_instruments) == (
            1 + len(BENCHMARK_INDEX_REGISTRY) + len(FUTURES_MAIN_SERIES_REGISTRY)
        )
