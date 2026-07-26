"""AkShareProvider 限流 + 批量拉取测试。

使用 monkeypatch 替换 ``_fetch_sync`` 为可控的 mock,验证:
- 信号量限制并发
- 请求间隔生效
- 重试退避
- 批量拉取进度回调
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from finboard_data.akshare_provider import AkShareProvider
from finboard_data.cache import CacheIOStats, CacheMetadata
from finboard_data.yfinance_provider import YFinanceProvider
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL_A = Symbol(code="510300.SH", market=Market.A_SHARE)
SYMBOL_B = Symbol(code="510050.SH", market=Market.A_SHARE)
SYMBOL_C = Symbol(code="159915.SZ", market=Market.A_SHARE)


def _make_bar(sym: Symbol, day: int, close: str = "10.00") -> Bar:
    return Bar(
        symbol=sym,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, 1 + day, tzinfo=UTC),
        open=Decimal("10.00"),
        high=Decimal("10.50"),
        low=Decimal("9.50"),
        close=Decimal(close),
        volume=Decimal("1000000"),
        amount=Decimal("10000000"),
    )


class TestRateLimiting:
    """限流参数验证。"""

    @pytest.mark.asyncio
    async def test_request_interval_enforced(self) -> None:
        """两次请求间隔 >= request_interval。"""
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=1,
            request_interval=0.3,
        )
        call_times: list[float] = []

        def mock_fetch_sync(symbol, period, start, end, adjust):
            call_times.append(time.monotonic())
            return [_make_bar(symbol, 0)]

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            await provider.fetch_bars(SYMBOL_A, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 2))
            await provider.fetch_bars(SYMBOL_B, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 2))

        assert len(call_times) == 2
        gap = call_times[1] - call_times[0]
        assert gap >= 0.25, f"请求间隔 {gap:.3f}s < 0.25s(预期 >= 0.3s)"

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self) -> None:
        """信号量限制并发请求数。"""
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=2,
            request_interval=0.0,
        )
        concurrent = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        async def mock_fetch_with_retry(symbol, period, start, end, adjust):
            nonlocal concurrent, max_concurrent
            async with lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.1)
            async with lock:
                concurrent -= 1
            return [_make_bar(symbol, 0)]

        with patch.object(provider, "_fetch_with_retry", mock_fetch_with_retry):
            await asyncio.gather(
                *[
                    provider._fetch_from_akshare(
                        s, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 2), "qfq"
                    )
                    for s in [SYMBOL_A, SYMBOL_B, SYMBOL_C, SYMBOL_A, SYMBOL_B]
                ]
            )

        assert max_concurrent <= 2, f"并发 {max_concurrent} > 2"

    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        """网络错误时重试。"""
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=1,
            request_interval=0.0,
            max_retries=2,
            retry_backoff=0.05,
        )
        call_count = 0

        def mock_fetch_sync(symbol, period, start, end, adjust):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("simulated network error")
            return [_make_bar(symbol, 0)]

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            bars = await provider.fetch_bars(
                SYMBOL_A, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 2)
            )

        assert len(bars) == 1
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self) -> None:
        """重试次数耗尽后抛出原始异常。"""
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=1,
            request_interval=0.0,
            max_retries=1,
            retry_backoff=0.01,
        )

        def mock_fetch_sync(symbol, period, start, end, adjust):
            raise ConnectionError("persistent error")

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            with pytest.raises(ConnectionError, match="persistent error"):
                await provider.fetch_bars(
                    SYMBOL_A, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 2)
                )


class TestFetchBatch:
    """批量拉取。"""

    @pytest.mark.asyncio
    async def test_batch_returns_all_symbols(self) -> None:
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=2,
            request_interval=0.0,
        )

        def mock_fetch_sync(symbol, period, start, end, adjust):
            return [_make_bar(symbol, 0), _make_bar(symbol, 1)]

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            results = await provider.fetch_bars_batch(
                [SYMBOL_A, SYMBOL_B, SYMBOL_C],
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 3),
            )

        assert set(results.keys()) == {"510300.SH", "510050.SH", "159915.SZ"}
        for bars in results.values():
            assert len(bars) == 2

    @pytest.mark.parametrize("provider_class", [AkShareProvider, YFinanceProvider])
    @pytest.mark.asyncio
    async def test_materialized_batch_rejects_all_market_size(
        self,
        provider_class: type[AkShareProvider] | type[YFinanceProvider],
    ) -> None:
        """禁止把全市场 Bars 全部保留在结果字典中导致 OOM。"""
        provider = provider_class(use_cache=False, request_interval=0.0)
        symbols = [
            Symbol(code=f"{index:06d}.SZ", market=Market.A_SHARE) for index in range(201)
        ]

        with pytest.raises(ValueError, match="最多支持 200 个标的"):
            await provider.fetch_bars_batch(
                symbols,
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 2),
            )

    @pytest.mark.asyncio
    async def test_batch_progress_callback(self) -> None:
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=1,
            request_interval=0.0,
        )
        progress: list[tuple[str, int, int]] = []

        def on_progress(code: str, done: int, total: int) -> None:
            progress.append((code, done, total))

        def mock_fetch_sync(symbol, period, start, end, adjust):
            return [_make_bar(symbol, 0)]

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            await provider.fetch_bars_batch(
                [SYMBOL_A, SYMBOL_B, SYMBOL_C],
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 2),
                on_progress=on_progress,
            )

        assert len(progress) == 3
        assert all(p[2] == 3 for p in progress)
        assert {p[0] for p in progress} == {"510300.SH", "510050.SH", "159915.SZ"}
        assert [p[1] for p in progress] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_batch_partial_failure(self) -> None:
        """部分标的拉取失败,其余正常返回。"""
        provider = AkShareProvider(
            use_cache=False,
            max_concurrency=1,
            request_interval=0.0,
            max_retries=0,
        )

        def mock_fetch_sync(symbol, period, start, end, adjust):
            if symbol.code == "510050.SH":
                raise ConnectionError("simulated failure")
            return [_make_bar(symbol, 0)]

        with patch.object(provider, "_fetch_sync", mock_fetch_sync):
            results = await provider.fetch_bars_batch(
                [SYMBOL_A, SYMBOL_B, SYMBOL_C],
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 2),
            )

        assert len(results["510300.SH"]) == 1
        assert results["510050.SH"] == []
        assert len(results["159915.SZ"]) == 1

    @pytest.mark.asyncio
    async def test_batch_bounds_cache_io_concurrency(self) -> None:
        """网络并发限制必须覆盖网络请求之前的缓存读取。"""

        class SlowCache:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0

            async def read(
                self,
                symbol: Symbol,
                period: BarPeriod,
                adjust: str,
            ) -> list[Bar]:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                return [
                    Bar(
                        symbol=symbol,
                        period=period,
                        timestamp=datetime(2026, 7, 24, tzinfo=UTC),
                        open=Decimal("1"),
                        high=Decimal("1"),
                        low=Decimal("1"),
                        close=Decimal("1"),
                        volume=Decimal("1"),
                        amount=Decimal("1"),
                    )
                ]

            def io_stats(self) -> CacheIOStats:
                return CacheIOStats(0, 0, 0, 0)

        provider = AkShareProvider(use_cache=False, max_concurrency=3)
        cache = SlowCache()
        provider._cache = cache  # type: ignore[assignment]
        symbols = [Symbol(code=f"{index:06d}.SZ", market=Market.A_SHARE) for index in range(100)]

        await provider.fetch_bars_batch(
            symbols,
            BarPeriod.D1,
            date(2026, 7, 1),
            date(2026, 7, 26),
        )

        assert cache.maximum <= 3

    @pytest.mark.asyncio
    async def test_cache_update_batch_reads_only_metadata_when_complete(self) -> None:
        """批量更新完整缓存时不得解码历史 bars。"""

        class MetadataCache:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.full_reads = 0

            async def metadata_for(
                self,
                symbol: Symbol,
                period: BarPeriod,
                adjust: str,
            ) -> CacheMetadata:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                return CacheMetadata(
                    bar_count=100,
                    first_date=date(2024, 1, 1),
                    last_date=date(2026, 7, 24),
                    file_size=1024,
                )

            async def read(
                self,
                symbol: Symbol,
                period: BarPeriod,
                adjust: str,
            ) -> list[Bar]:
                self.full_reads += 1
                return []

            def io_stats(self) -> CacheIOStats:
                return CacheIOStats(0, 0, 0, 0)

        provider = AkShareProvider(use_cache=False, max_concurrency=3)
        cache = MetadataCache()
        provider._cache = cache  # type: ignore[assignment]
        symbols = [Symbol(code=f"{index:06d}.SZ", market=Market.A_SHARE) for index in range(100)]

        results = await provider.update_cache_batch(
            symbols,
            BarPeriod.D1,
            date(2015, 1, 1),
            date(2026, 7, 26),
        )

        assert all(results.values())
        assert cache.maximum <= 3
        assert cache.full_reads == 0
