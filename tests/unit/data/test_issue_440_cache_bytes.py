"""ParquetCache 读缓存字节上限单元测试(issue #440)。

锁定字节口径:条目按「元素数 x 每元素实测平均字节常数」近似计量,超预算
按 LRU 从最旧淘汰、命中刷新新近度(move_to_end)、write/merge 后失效、
单条目超总预算不缓存(与既有 elements 口径同语义)、None = 不限与 #287
行为逐位一致。临时目录走 pytest tmp_path。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_data.cache import (
    _READ_CACHE_BYTES_PER_BAR,
    _READ_CACHE_BYTES_PER_CLOSE_COLUMN,
    _READ_CACHE_BYTES_PER_POINT,
    ParquetCache,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

_SYMBOL_A = Symbol(code="510300.SH", market=Market.A_SHARE)
_SYMBOL_B = Symbol(code="600519.SH", market=Market.A_SHARE)
_SYMBOL_C = Symbol(code="000001.SZ", market=Market.A_SHARE)


def _bars(count: int, symbol: Symbol = _SYMBOL_A) -> list[Bar]:
    base = datetime(2024, 1, 2, tzinfo=UTC)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=base + timedelta(days=offset),
            open=Decimal("10.00") + offset,
            high=Decimal("10.50") + offset,
            low=Decimal("9.50") + offset,
            close=Decimal("10.10") + offset,
            volume=Decimal(1000 + offset),
            amount=Decimal("10000.5"),
            source="fixed_sample",
        )
        for offset in range(count)
    ]


async def _seed(cache_dir: Path, bars: list[Bar]) -> None:
    cache = ParquetCache(cache_dir)
    await cache.write(bars[0].symbol, BarPeriod.D1, "qfq", bars)


@pytest.mark.asyncio
class TestByteAccounting:
    async def test_bytes_accumulate_per_entry_kind(self, tmp_path: Path) -> None:
        bars = _bars(6)
        await _seed(tmp_path, bars)
        cache = ParquetCache(tmp_path)

        await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        info_bars = cache.read_cache_info()
        assert info_bars["entries"] == 1
        assert info_bars["bytes"] == 6 * _READ_CACHE_BYTES_PER_BAR
        assert info_bars["elements"] == 6

        # 同一文件、不同 kind 的条目分键缓存,字节按各自常数累计。
        await cache.read_close_points(_SYMBOL_A, BarPeriod.D1, "qfq")
        info_points = cache.read_cache_info()
        assert info_points["entries"] == 2
        assert (
            info_points["bytes"]
            == 6 * _READ_CACHE_BYTES_PER_BAR + 6 * _READ_CACHE_BYTES_PER_POINT
        )

    async def test_close_columns_bytes_accounting(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(5))
        cache = ParquetCache(tmp_path)
        columns = await cache.read_close_columns(_SYMBOL_A, BarPeriod.D1, "qfq")
        info = cache.read_cache_info()
        assert len(columns.dates) == 5
        assert info["bytes"] == 5 * _READ_CACHE_BYTES_PER_CLOSE_COLUMN


@pytest.mark.asyncio
class TestByteBudgetEviction:
    async def test_evicts_oldest_over_byte_budget(self, tmp_path: Path) -> None:
        budget = 8 * _READ_CACHE_BYTES_PER_BAR  # 恰好容纳两个 4 根条目
        for symbol in (_SYMBOL_A, _SYMBOL_B, _SYMBOL_C):
            await _seed(tmp_path, _bars(4, symbol))
        cache = ParquetCache(tmp_path, read_cache_max_bytes=budget)

        first = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")  # miss,缓存 A
        second = await cache.read(_SYMBOL_B, BarPeriod.D1, "qfq")  # miss,A+B 恰好达标
        # 命中 A 刷新新近度(move_to_end):后续超预算时淘汰的是 B 而非 A。
        hit = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        assert hit == first
        third = await cache.read(_SYMBOL_C, BarPeriod.D1, "qfq")  # miss,挤出 B

        info = cache.read_cache_info()
        assert info == {
            "entries": 2,
            "elements": 8,
            "bytes": 8 * _READ_CACHE_BYTES_PER_BAR,
            "hits": 1,
        }
        # C 正常直读(4 根);B 已被逐出,重读与首次逐值一致。
        assert len(third) == 4
        # A 因命中刷新新近度而保留:再读仍命中。
        again_a = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        assert again_a == first
        assert cache.read_cache_info()["hits"] == 2
        # B 重读为 miss,命中计数不再增加。
        again_b = await cache.read(_SYMBOL_B, BarPeriod.D1, "qfq")
        assert again_b == second
        assert cache.read_cache_info()["hits"] == 2

    async def test_single_entry_over_budget_not_cached(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(10))
        cache = ParquetCache(tmp_path, read_cache_max_bytes=4 * _READ_CACHE_BYTES_PER_BAR)
        first = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        second = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        # 值正确性不受影响,但条目超预算不缓存(与 elements 口径同语义)。
        assert first == second
        info = cache.read_cache_info()
        assert info["entries"] == 0
        assert info["bytes"] == 0
        assert info["hits"] == 0

    async def test_zero_byte_budget_disables_cache(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(5))
        cache = ParquetCache(tmp_path, read_cache_max_bytes=0)
        first = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        second = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        assert first == second
        assert cache.read_cache_info()["hits"] == 0
        assert cache.read_cache_info()["bytes"] == 0

    async def test_negative_byte_budget_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="read_cache_max_bytes 必须 >= 0"):
            ParquetCache(tmp_path, read_cache_max_bytes=-1)

    async def test_elements_cap_still_applies_alongside_bytes(
        self, tmp_path: Path
    ) -> None:
        # 字节上限极大时,既有元素数上限照常驱逐(两口径并存)。
        await _seed(tmp_path, _bars(4, _SYMBOL_A))
        await _seed(tmp_path, _bars(4, _SYMBOL_B))
        cache = ParquetCache(
            tmp_path,
            read_cache_max_elements=6,
            read_cache_max_bytes=10**12,
        )
        await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        await cache.read(_SYMBOL_B, BarPeriod.D1, "qfq")
        info = cache.read_cache_info()
        assert info["entries"] == 1
        assert info["elements"] == 4
        assert info["bytes"] == 4 * _READ_CACHE_BYTES_PER_BAR

    async def test_default_none_is_unlimited(self, tmp_path: Path) -> None:
        for symbol in (_SYMBOL_A, _SYMBOL_B, _SYMBOL_C):
            await _seed(tmp_path, _bars(5, symbol))
        cache = ParquetCache(tmp_path)  # 默认 read_cache_max_bytes=None
        for symbol in (_SYMBOL_A, _SYMBOL_B, _SYMBOL_C):
            await cache.read(symbol, BarPeriod.D1, "qfq")
        info = cache.read_cache_info()
        assert info["entries"] == 3
        assert info["elements"] == 15
        assert info["bytes"] == 15 * _READ_CACHE_BYTES_PER_BAR


@pytest.mark.asyncio
class TestByteBudgetInvalidation:
    async def test_write_resets_byte_accounting(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(4))
        cache = ParquetCache(tmp_path)
        assert len(await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")) == 4
        assert cache.read_cache_info()["bytes"] == 4 * _READ_CACHE_BYTES_PER_BAR

        await cache.write(_SYMBOL_A, BarPeriod.D1, "qfq", _bars(7))

        # write 后同路径条目被显式丢弃,字节计数同步归零。
        assert cache.read_cache_info()["bytes"] == 0
        refreshed = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        assert len(refreshed) == 7
        assert cache.read_cache_info()["bytes"] == 7 * _READ_CACHE_BYTES_PER_BAR

    async def test_merge_resets_byte_accounting(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(4))
        cache = ParquetCache(tmp_path)
        assert len(await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")) == 4

        await cache.merge(_SYMBOL_A, BarPeriod.D1, "qfq", _bars(6))

        assert cache.read_cache_info()["bytes"] == 0
        refreshed = await cache.read(_SYMBOL_A, BarPeriod.D1, "qfq")
        assert len(refreshed) == 6
        assert cache.read_cache_info()["bytes"] == 6 * _READ_CACHE_BYTES_PER_BAR


class TestSettingsPassThrough:
    def test_providers_forward_byte_cap_to_cache(self, tmp_path: Path) -> None:
        from finboard_data import AkShareProvider, YFinanceProvider

        akshare = AkShareProvider(
            cache_dir=tmp_path, use_cache=True, read_cache_max_bytes=123
        )
        assert akshare._cache is not None
        assert akshare._cache._read_cache_max_bytes == 123

        yfinance = YFinanceProvider(
            cache_dir=tmp_path, use_cache=True, read_cache_max_bytes=456
        )
        assert yfinance._cache is not None
        assert yfinance._cache._read_cache_max_bytes == 456

    def test_providers_default_keep_unlimited(self, tmp_path: Path) -> None:
        from finboard_data import AkShareProvider

        provider = AkShareProvider(cache_dir=tmp_path, use_cache=True)
        assert provider._cache is not None
        assert provider._cache._read_cache_max_bytes is None

    def test_settings_field_default_none(self) -> None:
        from finboard_app.config import Settings

        assert Settings.model_fields["read_cache_max_bytes"].default is None
