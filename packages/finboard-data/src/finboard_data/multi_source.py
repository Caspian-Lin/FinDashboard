"""多源行情拉取 + 入口质量门。

在拉取阶段就运行质量校验:
1. 从主源拉取 bar 列表
2. 用 :class:`BarQualityChecker` 检查 OHLCV 异常
3. 如果主源有异常 bar,用备用源重拉相同日期段
4. 对异常日期的 bar 做交叉校验,优先采纳无异常的源
5. 将修正后的 bar 写回 parquet 缓存

这样数据质量问题在入口就被拦截和修复,不会堆积到发布阶段。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import structlog

from finboard_data.quality import BarQualityChecker, BarQualityResult
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """单标的拉取 + 质量校验结果。"""

    symbol: str
    bars: list[Bar]
    quality: BarQualityResult
    primary_source: str
    fallback_used: bool
    fallback_source: str | None
    corrected_dates: tuple[date, ...] = ()
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and self.quality.passed


@dataclass(frozen=True, slots=True)
class BatchFetchResult:
    """批量拉取结果汇总。"""

    results: tuple[FetchResult, ...]
    total_symbols: int
    passed: int
    failed: int
    fallback_used: int

    @property
    def failed_symbols(self) -> tuple[FetchResult, ...]:
        return tuple(r for r in self.results if not r.passed)


type BarFetcher = Callable[
    [Symbol, BarPeriod, date, date, str],
    Awaitable[list[Bar]],
]


class MultiSourceBarFetcher:
    """多源行情拉取器,在入口做质量校验和自动换源修复。

    Parameters
    ----------
    fetchers
        数据源名称到拉取函数的映射。拉取函数签名为
        ``(symbol, period, start, end, adjust) -> list[Bar]``。
    primary
        主源名称(默认 ``"akshare"``)。
    """

    def __init__(
        self,
        fetchers: dict[str, BarFetcher],
        *,
        primary: str = "akshare",
        max_anomaly_ratio: Decimal = Decimal("0.001"),
    ) -> None:
        if primary not in fetchers:
            raise ValueError(f"primary source '{primary}' not in fetchers")
        self._fetchers = fetchers
        self._primary = primary
        self._checker = BarQualityChecker()
        self._max_anomaly_ratio = max_anomaly_ratio

    async def fetch_and_validate(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> FetchResult:
        """拉取单个标的并做质量校验,必要时换源修复。"""
        primary_fn = self._fetchers[self._primary]

        try:
            primary_bars = await primary_fn(symbol, period, start, end, adjust)
        except Exception as exc:
            logger.warning(
                "multi_source.primary_failed",
                symbol=symbol.code,
                source=self._primary,
                error=str(exc),
            )
            return await self._try_fallback_all(
                symbol, period, start, end, adjust, error=str(exc)
            )

        qr = self._checker.check(primary_bars, symbol=symbol.code)

        if qr.passed or self._ratio_ok(qr):
            return FetchResult(
                symbol=symbol.code,
                bars=primary_bars,
                quality=qr,
                primary_source=self._primary,
                fallback_used=False,
                fallback_source=None,
            )

        return await self._cross_validate(
            symbol, period, start, end, adjust,
            primary_bars=primary_bars,
            primary_qr=qr,
        )

    async def fetch_batch(
        self,
        symbols: list[Symbol],
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
        max_concurrency: int = 3,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> BatchFetchResult:
        """批量拉取并校验。"""
        sem = asyncio.Semaphore(max_concurrency)
        results: list[FetchResult] = []
        total = len(symbols)

        async def _one(idx: int, sym: Symbol) -> FetchResult:
            async with sem:
                if on_progress:
                    on_progress(idx, total, sym.code)
                return await self.fetch_and_validate(sym, period, start, end, adjust=adjust)

        tasks = [_one(i, s) for i, s in enumerate(symbols)]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)

        results.sort(key=lambda r: r.symbol)
        passed = sum(1 for r in results if r.passed)
        fallback = sum(1 for r in results if r.fallback_used)
        return BatchFetchResult(
            results=tuple(results),
            total_symbols=total,
            passed=passed,
            failed=total - passed,
            fallback_used=fallback,
        )

    async def _cross_validate(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
        *,
        primary_bars: list[Bar],
        primary_qr: BarQualityResult,
    ) -> FetchResult:
        """对主源的异常 bar 用备用源交叉校验。"""
        anomaly_dates = set(primary_qr.anomaly_dates)
        corrected: list[date] = []
        merged = list(primary_bars)

        for src_name, fn in self._fetchers.items():
            if src_name == self._primary:
                continue
            try:
                alt_bars = await fn(symbol, period, start, end, adjust)
            except Exception as exc:
                logger.info(
                    "multi_source.fallback_unavailable",
                    symbol=symbol.code,
                    source=src_name,
                    error=str(exc),
                )
                continue

            alt_by_date = {b.timestamp.date(): b for b in alt_bars}
            alt_anomaly_dates: set[date] = set()
            for d, bar in alt_by_date.items():
                if d in anomaly_dates:
                    reasons = self._checker._check_bar(bar)
                    if not reasons:
                        idx = next(
                            (i for i, b in enumerate(merged)
                             if b.timestamp.date() == d),
                            None,
                        )
                        if idx is not None:
                            bar_with_src = Bar(
                                symbol=bar.symbol,
                                period=bar.period,
                                timestamp=bar.timestamp,
                                open=bar.open,
                                high=bar.high,
                                low=bar.low,
                                close=bar.close,
                                volume=bar.volume,
                                amount=bar.amount,
                                source=src_name,
                            )
                            merged[idx] = bar_with_src
                            corrected.append(d)
                    else:
                        alt_anomaly_dates.add(d)

            if corrected:
                logger.info(
                    "multi_source.corrected",
                    symbol=symbol.code,
                    primary=self._primary,
                    fallback=src_name,
                    corrected_count=len(corrected),
                    still_bad=len(anomaly_dates - set(corrected)),
                )
                break

        final_qr = self._checker.check(merged, symbol=symbol.code)
        return FetchResult(
            symbol=symbol.code,
            bars=merged,
            quality=final_qr,
            primary_source=self._primary,
            fallback_used=bool(corrected),
            fallback_source=src_name if corrected else None,
            corrected_dates=tuple(sorted(corrected)),
        )

    async def _try_fallback_all(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        adjust: str,
        *,
        error: str,
    ) -> FetchResult:
        """主源整体失败时,尝试用备用源拉取全量。"""
        for src_name, fn in self._fetchers.items():
            if src_name == self._primary:
                continue
            try:
                bars = await fn(symbol, period, start, end, adjust)
                qr = self._checker.check(bars, symbol=symbol.code)
                logger.info(
                    "multi_source.fallback_primary_error",
                    symbol=symbol.code,
                    primary=self._primary,
                    fallback=src_name,
                    bars=len(bars),
                )
                return FetchResult(
                    symbol=symbol.code,
                    bars=bars,
                    quality=qr,
                    primary_source=self._primary,
                    fallback_used=True,
                    fallback_source=src_name,
                    error=None,
                )
            except Exception as exc:
                logger.info(
                    "multi_source.fallback_failed",
                    symbol=symbol.code,
                    source=src_name,
                    error=str(exc),
                )

        return FetchResult(
            symbol=symbol.code,
            bars=[],
            quality=BarQualityResult(
                symbol=symbol.code,
                total_bars=0,
                anomaly_dates=(),
                anomalies=(),
                duplicate_count=0,
                sources=(),
            ),
            primary_source=self._primary,
            fallback_used=False,
            fallback_source=None,
            error=f"primary({self._primary}): {error}; all fallbacks also failed",
        )

    def _ratio_ok(self, qr: BarQualityResult) -> bool:
        """异常比例在可接受范围内(无需换源)。"""
        return qr.anomaly_ratio <= self._max_anomaly_ratio
