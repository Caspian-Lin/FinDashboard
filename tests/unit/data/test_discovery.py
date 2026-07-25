"""UniverseDiscovery + 代码归一化测试。"""

from __future__ import annotations

import pytest

from finboard_data.discovery import normalize_a_share_code
from finboard_shared.types import InstrumentType, Market


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
        assert info.instrument_type == InstrumentType.ETF

    def test_universe_discovery_class_exists(self) -> None:
        from finboard_data.discovery import UniverseDiscovery

        d = UniverseDiscovery()
        assert hasattr(d, "discover_a_shares")
        assert hasattr(d, "discover_a_etfs")
        assert hasattr(d, "discover_all")
