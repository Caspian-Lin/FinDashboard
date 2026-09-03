"""ParquetCache 进程内读缓存单元测试(issue #287)。

锁定三层不变量:缓存命中与直读逐值相等、write 后缓存失效、命中不改变
io_stats 的真实磁盘 I/O 口径。临时目录走仓库内 ``.pytest-tmp``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

import finboard_data.cache as finboard_cache_module
from finboard_data.cache import ParquetCache
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

_SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


@pytest.fixture(autouse=True)
def _unfiltered_structlog():
    """隔离 ``setup_logging`` 对 structlog 的全局污染,保 debug 断言确定性。

    CI 从仓库根跑全量(集成测试按字母序先于 unit),任何先行的测试调用
    ``setup_logging``(INFO 级 ``make_filtering_bound_logger`` +
    ``cache_logger_on_first_use=True``)后,debug 事件在 wrapper 层即被丢弃,
    ``capture_logs`` 永远抓空。本文件断言的恰是 debug 日志内容(issue #287
    AC「debug 日志带 cache_hit 字段」),故测试期重置为无级别过滤 + 不缓存
    并重建模块 logger(旧 proxy 可能已缓存被过滤的 wrapper),结束恢复原配置。
    """
    saved_config = structlog.get_config()
    saved_logger = finboard_cache_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    finboard_cache_module.logger = structlog.get_logger("finboard_data.cache")
    yield
    finboard_cache_module.logger = saved_logger
    structlog.configure(**saved_config)


def _bars(count: int, *, source: str = "fixed_sample") -> list[Bar]:
    base = datetime(2024, 1, 2, tzinfo=UTC)
    return [
        Bar(
            symbol=_SYMBOL,
            period=BarPeriod.D1,
            timestamp=base + timedelta(days=offset),
            open=Decimal("10.00") + offset,
            high=Decimal("10.50") + offset,
            low=Decimal("9.50") + offset,
            close=Decimal("10.10") + offset,
            volume=Decimal(1000 + offset),
            amount=Decimal("10000.5"),
            source=source,
        )
        for offset in range(count)
    ]


async def _seed(cache_dir: Path, bars: list[Bar]) -> None:
    cache = ParquetCache(cache_dir)
    await cache.write(_SYMBOL, BarPeriod.D1, "qfq", bars)


@pytest.mark.asyncio
class TestReadCacheHitEquivalence:
    async def test_second_read_equals_first_and_hits_cache(self, tmp_path: Path) -> None:
        bars = _bars(5)
        await _seed(tmp_path, bars)
        cache = ParquetCache(tmp_path)

        first = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        baseline_io = cache.io_stats()
        with capture_logs() as logs:
            second = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")

        # 逐值相等(Bar 为 frozen dataclass,直接比较)。
        assert second == first
        # 命中不再触盘:read_ops/read_bytes 停留在首次读取的水平。
        assert cache.io_stats() == baseline_io
        assert cache.read_cache_info()["hits"] == 1
        # debug 日志可见 cache hit(AC 要求)。
        read_logs = [item for item in logs if item.get("event") == "parquet_cache.read"]
        assert len(read_logs) == 1
        assert read_logs[0]["cache_hit"] is True

    async def test_first_read_logs_cache_miss(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(5))
        cache = ParquetCache(tmp_path)
        with capture_logs() as logs:
            await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        read_logs = [item for item in logs if item.get("event") == "parquet_cache.read"]
        assert read_logs[0]["cache_hit"] is False

    async def test_caller_mutation_does_not_corrupt_cache(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(5))
        cache = ParquetCache(tmp_path)
        first = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        first.pop()  # 就地修改返回列表不影响缓存
        again = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        assert len(again) == 5
        assert again[-1].close == Decimal("14.1")


@pytest.mark.asyncio
class TestReadCacheInvalidation:
    async def test_write_invalidates_read_cache(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(3))
        cache = ParquetCache(tmp_path)
        assert len(await cache.read(_SYMBOL, BarPeriod.D1, "qfq")) == 3

        await cache.write(_SYMBOL, BarPeriod.D1, "qfq", _bars(7))

        refreshed = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        assert len(refreshed) == 7
        assert cache.read_cache_info()["hits"] == 0

    async def test_merge_invalidates_via_write(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(3))
        cache = ParquetCache(tmp_path)
        assert len(await cache.read(_SYMBOL, BarPeriod.D1, "qfq")) == 3
        await cache.merge(_SYMBOL, BarPeriod.D1, "qfq", _bars(6))
        assert len(await cache.read(_SYMBOL, BarPeriod.D1, "qfq")) == 6


@pytest.mark.asyncio
class TestReadCacheBounds:
    async def test_lru_eviction_keeps_values_equal(self, tmp_path: Path) -> None:
        other = Symbol(code="600519.SH", market=Market.A_SHARE)
        cache = ParquetCache(tmp_path, read_cache_max_elements=6)
        await cache.write(_SYMBOL, BarPeriod.D1, "qfq", _bars(4))
        await cache.write(other, BarPeriod.D1, "qfq", _bars(4))

        first = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")  # miss,缓存 {A}
        second = await cache.read(other, BarPeriod.D1, "qfq")  # miss,挤出 A
        third = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")  # miss,挤出 B
        fourth = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")  # hit

        # 被逐出后重新直读,值与首次读取逐值相等。
        assert first == third == fourth
        assert len(second) == 4
        assert cache.read_cache_info() == {"entries": 1, "elements": 4, "hits": 1}

    async def test_zero_budget_disables_cache(self, tmp_path: Path) -> None:
        await _seed(tmp_path, _bars(5))
        cache = ParquetCache(tmp_path, read_cache_max_elements=0)
        first = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        second = await cache.read(_SYMBOL, BarPeriod.D1, "qfq")
        assert first == second
        assert cache.read_cache_info()["hits"] == 0


@pytest.mark.asyncio
class TestReadClosePointsCache:
    async def test_ranged_reads_hit_full_file_cache(self, tmp_path: Path) -> None:
        bars = _bars(6)
        await _seed(tmp_path, bars)
        cache = ParquetCache(tmp_path)
        start = bars[2].timestamp.date()
        end = bars[4].timestamp.date()

        direct = ParquetCache.read_close_points_sync(
            tmp_path / "510300.SH_1d_qfq.parquet",
            _SYMBOL,
            BarPeriod.D1,
            start,
            end,
        )
        first = await cache.read_close_points(
            _SYMBOL, BarPeriod.D1, "qfq", start=start, end=end
        )
        assert first == direct
        info_after_first = cache.read_cache_info()
        second = await cache.read_close_points(
            _SYMBOL, BarPeriod.D1, "qfq", start=start, end=end
        )
        # 第二次命中整文件点位缓存,内存裁剪结果与直读逐值相等。
        assert second == direct == first
        assert cache.read_cache_info()["hits"] == info_after_first["hits"] + 1

    async def test_full_range_and_none_range_equal(self, tmp_path: Path) -> None:
        bars = _bars(5)
        await _seed(tmp_path, bars)
        cache = ParquetCache(tmp_path)
        awaited = await cache.read_close_points(_SYMBOL, BarPeriod.D1, "qfq")
        direct = ParquetCache.read_close_points_sync(
            tmp_path / "510300.SH_1d_qfq.parquet",
            _SYMBOL,
            BarPeriod.D1,
            None,
            None,
        )
        assert awaited == direct
        assert len(awaited) == 5


@pytest.mark.asyncio
class TestReadBarsSyncColumnConversion:
    async def test_column_wise_read_matches_written_bars(self, tmp_path: Path) -> None:
        bars = _bars(4)
        await _seed(tmp_path, bars)
        path = tmp_path / "510300.SH_1d_qfq.parquet"
        parsed = ParquetCache.read_bars_sync(path, _SYMBOL, BarPeriod.D1)
        assert parsed == bars

    async def test_missing_optional_columns_use_defaults(self, tmp_path: Path) -> None:
        """缺 volume/amount/source 列时按 0/空串补齐(与逐行读取同语义)。"""
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / "510300.SH_1d_qfq.parquet"
        table = pa.table(
            {
                "timestamp": [datetime(2024, 1, 2, tzinfo=UTC)],
                "open": [10.0],
                "high": [10.5],
                "low": [9.5],
                "close": [10.1],
            }
        )
        pq.write_table(table, path)
        parsed = ParquetCache.read_bars_sync(path, _SYMBOL, BarPeriod.D1)
        assert len(parsed) == 1
        assert parsed[0].volume == Decimal("0")
        assert parsed[0].amount == Decimal("0")
        assert parsed[0].source == ""
