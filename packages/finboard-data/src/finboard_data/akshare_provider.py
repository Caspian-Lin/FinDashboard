"""``AkShareProvider`` —— 基于 akshare 的 A 股历史数据提供者。

akshare 免费、无需 token,覆盖 A 股日线 / 分钟线,是个人量化的首选数据源。

akshare 为同步库,所有调用通过 ``asyncio.to_thread`` 在线程池执行,
避免阻塞事件循环。

内置限流(信号量 + 请求间隔 + 重试退避),防止被 akshare 封 IP。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
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
    BarPeriod.D1: "daily",
    BarPeriod.M1: "1",
    BarPeriod.M5: "5",
    BarPeriod.M15: "15",
    BarPeriod.M30: "30",
    BarPeriod.H1: "60",
}

_ADJUST_MAP: dict[str, str] = {
    "qfq": "qfq",
    "hqfq": "hfq",
    "none": "",
}


class AkShareProvider:
    """akshare 历史数据提供者,带本地 parquet 缓存 + 限流。

    用法::

        provider = AkShareProvider(cache_dir="data_cache")
        bars = await provider.fetch_bars(symbol, BarPeriod.D1,
                                         start, end, adjust="qfq")

    批量拉取::

        results = await provider.fetch_bars_batch(
            [sym1, sym2, sym3], BarPeriod.D1, start, end,
            on_progress=lambda code, done, total: print(f"{done}/{total} {code}"),
        )

    限流参数:

    * ``max_concurrency``: 信号量,同时最多 N 个 akshare 请求在途
    * ``request_interval``: 两次请求间的最小间隔(秒),防封 IP
    * ``max_retries`` / ``retry_backoff``: 网络错误时指数退避重试
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        use_cache: bool = True,
        max_concurrency: int = 3,
        request_interval: float = 0.5,
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
            logger.debug(
                "akshare.cache_hit",
                symbol=symbol.code,
                count=len(cached),
            )
            return ParquetCache.filter_by_date(cached, start, end)

        logger.info(
            "akshare.fetching",
            symbol=symbol.code,
            period=period.value,
            start=str(start),
            end=str(end),
            cached=len(cached),
        )
        fetch_start = incremental_fetch_start(cached, start)
        fresh = await self._fetch_from_akshare(symbol, period, fetch_start, end, adjust)
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
        on_status: Callable[[str], None] | None = None,
    ) -> bool:
        """仅更新本地缓存;完整缓存只读 footer,不解码历史行情。"""
        if self._cache is None:
            return bool(await self.fetch_bars(symbol, period, start, end, adjust=adjust))

        if on_status is not None:
            on_status("checking_cache")
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
        if on_status is not None:
            on_status("fetching")
        fresh = await self._fetch_from_akshare(symbol, period, fetch_start, end, adjust)
        if not fresh:
            return metadata is not None and metadata.bar_count > 0
        if (
            metadata is not None
            and metadata.last_date is not None
            and fresh[-1].timestamp.date() <= metadata.last_date
        ):
            return True

        if on_status is not None:
            on_status("reading_cache")
        existing = await self._cache.read(symbol, period, adjust) if metadata is not None else []
        if on_status is not None:
            on_status("writing_cache")
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
        """批量拉取多标的数据,带限流 + 进度回调。

        :param on_progress: 回调 ``on_progress(symbol_code, done, total)``
        :returns: ``{symbol_code: [Bar, ...]}``;拉取失败的标的值为空列表
        """
        total = len(symbols)
        if total > 200:
            raise ValueError(
                "fetch_bars_batch 最多支持 200 个标的;全市场落盘请使用 update_cache_batch"
            )
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
                    logger.exception("akshare.batch_failed", symbol=sym.code)
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
                "akshare.batch_cache_io",
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
        on_status: Callable[[str, str], None] | None = None,
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
                    ok = await self.update_cache(
                        sym,
                        period,
                        start,
                        end,
                        adjust=adjust,
                        on_status=(
                            partial(on_status, sym.code)
                            if on_status is not None
                            else None
                        ),
                    )
                except Exception:
                    logger.exception("akshare.cache_update_failed", symbol=sym.code)
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
                "akshare.cache_update_io",
                symbols=total,
                workers=worker_count,
                read_ops=after.read_ops - before.read_ops,
                read_bytes=after.read_bytes - before.read_bytes,
                write_ops=after.write_ops - before.write_ops,
                write_bytes=after.write_bytes - before.write_bytes,
            )
        return results

    @staticmethod
    def _is_cache_complete(cached: list[Bar], start: date, end: date) -> bool:
        """简化判断:缓存非空且最后一条 >= end 即视为完整。

        精确的交易日对齐由 TradingCalendar 负责,这里用宽松判断避免
        引入 scheduler 依赖。回测引擎会在拿到数据后做进一步处理。
        """
        if not cached:
            return False
        last = cached[-1].timestamp.date()
        return last >= expected_last_bar_date(end)

    async def _fetch_from_akshare(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
    ) -> list[Bar]:
        """带信号量限流 + 请求间隔 + 重试退避的 akshare 调用。"""
        async with self._semaphore:
            await self._enforce_interval()
            return await self._fetch_with_retry(symbol, period, start, end, adjust)

    async def _enforce_interval(self) -> None:
        """保证两次 akshare 请求之间至少间隔 ``_request_interval`` 秒。"""
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
        """指数退避重试。"""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.to_thread(self._fetch_sync, symbol, period, start, end, adjust)
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = self._retry_backoff**attempt
                    logger.warning(
                        "akshare.fetch_retry",
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
        import akshare as ak

        code = symbol.code.split(".")[0]
        ak_period = _PERIOD_MAP.get(period)
        if ak_period is None:
            raise ValueError(f"akshare 不支持周期: {period}")
        ak_adjust = _ADJUST_MAP.get(adjust, "")

        if period == BarPeriod.D1:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period=ak_period,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=ak_adjust,
            )
        else:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=ak_period,
                start_date=f"{start.strftime('%Y-%m-%d')} 09:30:00",
                end_date=f"{end.strftime('%Y-%m-%d')} 15:00:00",
                adjust=ak_adjust,
            )

        bars: list[Bar] = []
        col_map = self._column_map(period)

        for _, row in df.iterrows():
            ts = self._parse_timestamp(row[col_map["datetime"]], period)
            bars.append(
                Bar(
                    symbol=symbol,
                    period=period,
                    timestamp=ts,
                    open=Decimal(str(row[col_map["open"]])),
                    high=Decimal(str(row[col_map["high"]])),
                    low=Decimal(str(row[col_map["low"]])),
                    close=Decimal(str(row[col_map["close"]])),
                    volume=Decimal(str(row[col_map["volume"]])),
                    amount=Decimal(
                        str(row[col_map["amount"]]) if col_map["amount"] in df.columns else 0
                    ),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        logger.info(
            "akshare.fetched",
            symbol=symbol.code,
            period=period.value,
            count=len(bars),
        )
        return bars

    @staticmethod
    def _column_map(period: BarPeriod) -> dict[str, str]:
        """akshare 返回的中文列名映射。

        日线: 日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量 / 成交额
        分钟: 时间 / 开盘 / 最高 / 最低 / 收盘 / 成交量 / 成交额
        """
        datetime_col = "日期" if period == BarPeriod.D1 else "时间"
        return {
            "datetime": datetime_col,
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
        }

    @staticmethod
    def _parse_timestamp(raw: object, period: BarPeriod) -> datetime:
        if isinstance(raw, str):
            if period == BarPeriod.D1:
                dt = datetime.strptime(raw, "%Y-%m-%d")
            else:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        elif isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.strptime(str(raw), "%Y-%m-%d")
        return dt.replace(tzinfo=UTC)
