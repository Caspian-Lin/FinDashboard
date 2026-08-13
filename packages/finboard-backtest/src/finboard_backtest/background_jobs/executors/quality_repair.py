"""``quality_repair`` 执行器 —— 批量缓存异常 bar 修复接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data.repair_cache_quality`` 同步阻塞路径。worker 领取
``kind=quality_repair`` 任务后,对每个标的读取本地缓存、用备用源重拉异常日期、
``BarQualityChecker`` 校验后写回 ``ParquetCache``。

保留 ``asyncio.Semaphore(3)`` 并发约束(对备用源的请求限流)。``result_ref = None``;
精细的逐标的修复报告(QualityReportOut)在迁移到通用队列后降级为 ``JobOut`` 进度,
不进入 ``result_ref``。

边界:只读写本地 ``data_cache`` 缓存目录,不连 broker / 不下实盘单 / 不修改持仓;
单并发由 ``kind_concurrency={"quality_repair": 1}`` 保证。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors._providers import (
    build_bar_provider,
    resolve_provider_name,
)

if TYPE_CHECKING:
    from finboard_backtest.background_jobs.executors._providers import SettingsFactory


class QualityRepairExecutor:
    """``kind=quality_repair`` 执行器。"""

    KIND = "quality_repair"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: SettingsFactory,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        symbols = job.payload.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ExecutorError(
                code="invalid_payload",
                summary="quality_repair 任务 payload 必须包含非空 symbols 列表",
                retryable=False,
                context={"job_id": job.job_id},
            )
        source_raw = job.payload.get("source")
        if source_raw is not None and not isinstance(source_raw, str):
            raise ExecutorError(
                code="invalid_payload",
                summary="source 必须是字符串",
                retryable=False,
                context={"job_id": job.job_id},
            )
        adjust = job.payload.get("adjust", "qfq")
        if not isinstance(adjust, str):
            raise ExecutorError(
                code="invalid_payload",
                summary="adjust 必须是字符串",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from finboard_data.cache import ParquetCache, make_symbol
        from finboard_data.quality import BarQualityChecker
        from finboard_shared.types import BarPeriod

        unique_codes = list(dict.fromkeys(symbols))
        cache = ParquetCache("data_cache")
        checker = BarQualityChecker()
        provider_name = resolve_provider_name(source_raw, self._settings_factory)
        provider = build_bar_provider(
            provider_name, self._settings_factory, use_cache=False
        )
        semaphore = asyncio.Semaphore(3)
        total = len(unique_codes)
        await progress(0, total, "quality_repair:start")

        async def _repair(code: str) -> bool:
            sym = make_symbol(code)
            try:
                bars = await cache.read(sym, BarPeriod.D1, adjust)
                if not bars:
                    return False
                before = checker.check(bars, symbol=code)
                by_date = {bar.timestamp.date(): bar for bar in bars}
                anomaly_dates = set(before.anomaly_dates)
                async with semaphore:
                    alternatives = await provider.fetch_bars(
                        sym,
                        BarPeriod.D1,
                        min(bar.timestamp.date() for bar in bars),
                        max(bar.timestamp.date() for bar in bars),
                        adjust=adjust,
                    )
                corrected = False
                for alternative in alternatives:
                    bar_date = alternative.timestamp.date()
                    current = by_date.get(bar_date)
                    if current is not None and bar_date not in anomaly_dates:
                        continue
                    if checker.check([alternative], symbol=code).passed:
                        by_date[bar_date] = replace(alternative, source=source_raw or provider_name)
                        if current is None or bar_date in anomaly_dates:
                            corrected = True
                repaired_bars = sorted(by_date.values(), key=lambda bar: bar.timestamp)
                if corrected or before.duplicate_count:
                    await cache.write(sym, BarPeriod.D1, adjust, repaired_bars)
                after = checker.check(repaired_bars, symbol=code)
                return bool(repaired_bars) and after.passed
            except Exception:
                return False

        for idx, code in enumerate(unique_codes, start=1):
            await _repair(code)
            await progress(idx, total, "quality_repair:repairing")

        await progress(total, total, "quality_repair:done")
        return JobResult(status="succeeded", result_ref=None, progress_total=total)


_: type[JobExecutor] = QualityRepairExecutor

__all__ = ["QualityRepairExecutor"]
