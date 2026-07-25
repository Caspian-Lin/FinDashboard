"""``YFinanceProvider`` —— 基于 yfinance 的历史数据提供者。

yfinance 使用 Yahoo Finance 数据源,网络稳定性优于 akshare(东方财富)。

符号映射:
* A 股上海(``.SH``)→ Yahoo ``.SS``
* A 股深圳(``.SZ``)→ Yahoo ``.SZ`` (不变)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

from finboard_data.cache import (
    ParquetCache,
    expected_last_bar_date,
    incremental_fetch_start,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)

_PERIOD_MAP: dict[BarPeriod, str] = {
    BarPeriod.D1: "1d",
    BarPeriod.M1: "1m",
    BarPeriod.M5: "5m",
    BarPeriod.M15: "15m",
    BarPeriod.M30: "30m",
    BarPeriod.H1: "60m",
}

_ADJUST_MAP: dict[str, str] = {
    "qfq": "adjust",
    "hqfq": "auto",
    "none": "raw",
}


def _to_yahoo_symbol(code: str) -> str:
    """``510300.SH`` → ``510300.SS``;``159915.SZ`` → ``159915.SZ``。"""
    if code.endswith(".SH"):
        return code[:-3] + ".SS"
    return code


class YFinanceProvider:
    """yfinance 历史数据提供者,带本地 parquet 缓存 + 限流。

    用法::

        provider = YFinanceProvider()
        bars = await provider.fetch_bars(symbol, BarPeriod.D1,
                                         start, end, adjust="qfq")
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        max_concurrency: int = 3,
        request_interval: float = 0.3,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        max_cache_io_concurrency: int = 1,
    ) -> None:
        if use_cache:
            dir_path = str(cache_dir) if cache_dir else "data_cache"
            self._cache: ParquetCache | None = ParquetCache(
                dir_path,
                max_io_concurrency=max_cache_io_concurrency,
            )
        else:
            self._cache = None

        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._request_interval = request_interval
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._interval_lock = asyncio.Lock()
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------ 单标的
    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        """拉取历史 K 线(优先读缓存,缺失时增量拉取)。"""
        cached: list[Bar] = []
        if self._cache is not None:
            cached = await self._cache.read(symbol, period, adjust)

        if self._is_cache_complete(cached, start, end):
            logger.debug("yfinance.cache_hit", symbol=symbol.code, count=len(cached))
            return ParquetCache.filter_by_date(cached, start, end)

        logger.info(
            "yfinance.fetching",
            symbol=symbol.code,
            period=period.value,
            start=str(start),
            end=str(end),
            cached=len(cached),
        )
        fetch_start = incremental_fetch_start(cached, start)
        fresh = await self._fetch_from_yfinance(symbol, period, fetch_start, end, adjust)
        if self._cache is not None and fresh:
            all_bars = await self._cache.merge(
                symbol,
                period,
                adjust,
                fresh,
                existing_bars=cached,
            )
        else:
            all_bars = cached or fresh

        return ParquetCache.filter_by_date(all_bars, start, end)

    async def update_cache(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> bool:
        """仅更新本地缓存;完整缓存只读 footer,不解码历史行情。"""
        if self._cache is None:
            return bool(await self.fetch_bars(symbol, period, start, end, adjust=adjust))

        metadata = await self._cache.metadata_for(symbol, period, adjust)
        expected_end = expected_last_bar_date(end)
        if (
            metadata is not None
            and metadata.bar_count > 0
            and metadata.last_date is not None
            and metadata.last_date >= expected_end
        ):
            return True

        fetch_start = start
        if metadata is not None and metadata.last_date is not None:
            fetch_start = max(start, metadata.last_date - timedelta(days=7))
        fresh = await self._fetch_from_yfinance(symbol, period, fetch_start, end, adjust)
        if not fresh:
            return metadata is not None and metadata.bar_count > 0
        if (
            metadata is not None
            and metadata.last_date is not None
            and fresh[-1].timestamp.date() <= metadata.last_date
        ):
            return True

        existing = await self._cache.read(symbol, period, adjust) if metadata is not None else []
        await self._cache.merge(
            symbol,
            period,
            adjust,
            fresh,
            existing_bars=existing,
        )
        return True

    # ------------------------------------------------------------------ 批量
    async def fetch_bars_batch(
        self,
        symbols: list[Symbol],
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, list[Bar]]:
        """批量拉取多标的数据,带限流 + 进度回调。"""
        total = len(symbols)
        if total == 0:
            return {}
        results: dict[str, list[Bar]] = {}
        done_count = 0
        queue: asyncio.Queue[Symbol] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        before = self._cache.io_stats() if self._cache is not None else None

        async def _worker() -> None:
            nonlocal done_count
            while True:
                try:
                    sym = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    bars = await self.fetch_bars(sym, period, start, end, adjust=adjust)
                except Exception:
                    logger.exception("yfinance.batch_failed", symbol=sym.code)
                    bars = []
                results[sym.code] = bars
                done_count += 1
                if on_progress is not None:
                    on_progress(sym.code, done_count, total)

        worker_count = min(self._max_concurrency, total)
        await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(worker_count)])

        if self._cache is not None and before is not None:
            after = self._cache.io_stats()
            logger.info(
                "yfinance.batch_cache_io",
                symbols=total,
                workers=worker_count,
                read_ops=after.read_ops - before.read_ops,
                read_bytes=after.read_bytes - before.read_bytes,
                write_ops=after.write_ops - before.write_ops,
                write_bytes=after.write_bytes - before.write_bytes,
            )
        return results

    async def update_cache_batch(
        self,
        symbols: list[Symbol],
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, bool]:
        """有界并发批量更新缓存,不在内存中保留历史 bars。"""
        total = len(symbols)
        if total == 0:
            return {}
        results: dict[str, bool] = {}
        done_count = 0
        queue: asyncio.Queue[Symbol] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        before = self._cache.io_stats() if self._cache is not None else None

        async def _worker() -> None:
            nonlocal done_count
            while True:
                try:
                    sym = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    ok = await self.update_cache(sym, period, start, end, adjust=adjust)
                except Exception:
                    logger.exception("yfinance.cache_update_failed", symbol=sym.code)
                    ok = False
                results[sym.code] = ok
                done_count += 1
                if on_progress is not None:
                    on_progress(sym.code, done_count, total)

        worker_count = min(self._max_concurrency, total)
        await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(worker_count)])

        if self._cache is not None and before is not None:
            after = self._cache.io_stats()
            logger.info(
                "yfinance.cache_update_io",
                symbols=total,
                workers=worker_count,
                read_ops=after.read_ops - before.read_ops,
                read_bytes=after.read_bytes - before.read_bytes,
                write_ops=after.write_ops - before.write_ops,
                write_bytes=after.write_bytes - before.write_bytes,
            )
        return results

    # ------------------------------------------------------------------ 限流
    @staticmethod
    def _is_cache_complete(cached: list[Bar], start: date, end: date) -> bool:
        if not cached:
            return False
        return cached[-1].timestamp.date() >= expected_last_bar_date(end)

    async def _fetch_from_yfinance(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        async with self._semaphore:
            await self._enforce_interval()
            return await self._fetch_with_retry(symbol, period, start, end, adjust)

    async def _enforce_interval(self) -> None:
        async with self._interval_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_request_time
            if elapsed < self._request_interval:
                await asyncio.sleep(self._request_interval - elapsed)
            self._last_request_time = loop.time()

    async def _fetch_with_retry(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.to_thread(self._fetch_sync, symbol, period, start, end, adjust)
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = self._retry_backoff**attempt
                    logger.warning(
                        "yfinance.fetch_retry",
                        symbol=symbol.code,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        wait=f"{wait:.1f}s",
                        error=str(exc),
                    )
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    def _fetch_sync(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        import yfinance as yf

        yahoo_symbol = _to_yahoo_symbol(symbol.code)
        yf_period = _PERIOD_MAP.get(period)
        if yf_period is None:
            raise ValueError(f"yfinance 不支持周期: {period}")

        # yfinance 的 end 是 exclusive,加一天
        end_str = (end + timedelta(days=1)).strftime("%Y-%m-%d")
        start_str = start.strftime("%Y-%m-%d")

        yf_adjust = adjust != "none"

        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(
            start=start_str,
            end=end_str,
            interval=yf_period,
            auto_adjust=yf_adjust,
        )

        if df is None or df.empty:
            logger.warning("yfinance.no_data", symbol=symbol.code, yahoo=yahoo_symbol)
            return []

        bars: list[Bar] = []
        for ts, row in df.iterrows():
            dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if isinstance(dt, datetime):
                if period == BarPeriod.D1:
                    # 日线 timestamp 表示交易日,统一为 UTC 零点,避免时区换算减一天。
                    dt = datetime.combine(dt.date(), datetime.min.time(), tzinfo=UTC)
                elif dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                else:
                    dt = dt.astimezone(UTC)
            else:
                dt = datetime.combine(dt, datetime.min.time(), tzinfo=UTC)

            bars.append(
                Bar(
                    symbol=symbol,
                    period=period,
                    timestamp=dt,
                    open=Decimal(str(row["Open"])),
                    high=Decimal(str(row["High"])),
                    low=Decimal(str(row["Low"])),
                    close=Decimal(str(row["Close"])),
                    volume=Decimal(str(row.get("Volume", 0))),
                    amount=Decimal("0"),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        logger.info(
            "yfinance.fetched",
            symbol=symbol.code,
            yahoo=yahoo_symbol,
            period=period.value,
            count=len(bars),
        )
        return bars
