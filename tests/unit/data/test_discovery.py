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

        monkeypatch.setattr(d, "discover_a_shares", _fake_stocks)
        monkeypatch.setattr(d, "discover_a_etfs", _fake_etfs)
        all_instruments = await d.discover_all()
        by_type = {item.instrument_type for item in all_instruments}
        assert by_type == {InstrumentType.STOCK, InstrumentType.ETF, InstrumentType.INDEX}
        assert len(all_instruments) == 2 + len(BENCHMARK_INDEX_REGISTRY)
