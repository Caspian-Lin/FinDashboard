"""``bulk_download`` 执行器 —— 全市场批量历史数据拉取接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data._run_download`` 内存态 asyncio 任务。worker 领取
``kind=bulk_download`` 任务后,从 payload 重建筛选条件与 provider,调
``provider.update_cache_batch`` 更新 Parquet 缓存。

边界:只写本地 ``data_cache`` 缓存目录,不连 broker / 不下实盘单 / 不修改持仓;
单并发由 worker ``kind_concurrency={"bulk_download": 1}`` 保证(避免对上游行情源
造成并发压力)。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors._progress import make_sync_progress
from finboard_backtest.background_jobs.executors._providers import (
    build_bar_provider,
    resolve_provider_name,
)

if TYPE_CHECKING:
    from finboard_backtest.background_jobs.executors._providers import (
        SettingsFactory,
    )


class BulkDownloadExecutor:
    """``kind=bulk_download`` 执行器。"""

    KIND = "bulk_download"

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
        market = _require_str(job, "market")
        source = _require_str(job, "source")
        start = _parse_date(job, "start")
        instrument_type_raw = job.payload.get("instrument_type")
        exchange = job.payload.get("exchange")
        listing_boards = job.payload.get("listing_boards")
        if instrument_type_raw is not None and not isinstance(instrument_type_raw, str):
            raise ExecutorError(
                code="invalid_payload",
                summary="instrument_type 必须是字符串",
                retryable=False,
                context={"job_id": job.job_id},
            )
        if listing_boards is not None and not isinstance(listing_boards, list):
            raise ExecutorError(
                code="invalid_payload",
                summary="listing_boards 必须是列表",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from finboard_data.cache import make_symbol
        from finboard_persistence import InstrumentRepository
        from finboard_shared.types import BarPeriod

        await progress(0, None, "bulk_download:resolving_instruments")
        # 1. 从 instruments 表选出活跃标的
        async with self._session_maker() as session:
            repo = InstrumentRepository(session)
            instruments, _ = await repo.list_active(
                market=market,
                instrument_type=instrument_type_raw,
                exchange=exchange if isinstance(exchange, str) else None,
                listing_boards=listing_boards if isinstance(listing_boards, list) else None,
                limit=999999,
            )
            await session.commit()

        if not instruments:
            raise ExecutorError(
                code="no_instruments",
                summary="未找到匹配的标的(请先同步)",
                retryable=False,
                context={"job_id": job.job_id, "market": market},
            )

        provider_name = resolve_provider_name(source, self._settings_factory)
        _validate_tushare_scope(provider_name, instruments)
        provider = build_bar_provider(provider_name, self._settings_factory)

        sym_objs = [make_symbol(ins.code) for ins in instruments]
        end = date.today()
        total_holder: dict[str, int | None] = {"total": None}
        on_progress = make_sync_progress(
            progress, phase_prefix="bulk_download", total_holder=total_holder
        )

        await progress(0, len(sym_objs), "bulk_download:fetching")
        results = await provider.update_cache_batch(
            sym_objs,
            BarPeriod.D1,
            start,
            end,
            on_progress=on_progress,
        )
        success = sum(results.values())
        failed = len(sym_objs) - success
        await progress(len(sym_objs), len(sym_objs), "bulk_download:done")
        if failed == len(sym_objs) and success == 0:
            raise ExecutorError(
                code="all_symbols_failed",
                summary=f"批量拉取全部 {failed} 个标的均失败",
                retryable=False,
                context={"job_id": job.job_id, "failed": failed},
            )
        return JobResult(
            status="succeeded",
            result_ref=None,
            progress_total=len(sym_objs),
        )


def _validate_tushare_scope(provider_name: str, instruments: Sequence[object]) -> None:
    """复刻 ``data._validate_bulk_provider_scope``:tushare 批量仅支持 A 股股票与转债。

    可转债(issue #265)走 2000 积分档的 ``cb_daily`` 专属接口
    (TushareBarProvider 按代码规则分流),与 ETF / 指数「2000 积分拉不到」
    的边界不同,因此放行;期货(issue #267)tushare 侧不接线(fut_daily
    属另档积分),走 akshare 新浪主连。tushare 拒绝行为对其余非股票标的
    保持不变(具名 tushare_scope_mismatch,不静默换源)。
    """
    if provider_name != "tushare":
        return
    incompatible = [
        getattr(ins, "code", "?")
        for ins in instruments
        if getattr(ins, "market", None) != "a_share"
        or getattr(ins, "instrument_type", None) not in ("stock", "convertible")
    ]
    if incompatible:
        raise ExecutorError(
            code="tushare_scope_mismatch",
            summary=(
                "Tushare 批量任务仅支持 A 股股票与可转债;"
                "ETF / 指数 / 期货请另建任务选 akshare"
            ),
            retryable=False,
            context={"sample": incompatible[:5]},
        )


def _require_str(job: JobRecord, key: str) -> str:
    raw = job.payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"bulk_download 任务 payload 必须包含合法 {key}",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


def _parse_date(job: JobRecord, key: str) -> date:
    raw = job.payload.get(key)
    if not isinstance(raw, str):
        raise ExecutorError(
            code="invalid_payload",
            summary=f"bulk_download 任务 payload 缺少 {key}",
            retryable=False,
            context={"job_id": job.job_id},
        )
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"bulk_download 任务 payload {key} 不是合法日期",
            retryable=False,
            context={"job_id": job.job_id},
        ) from exc


_: type[JobExecutor] = BulkDownloadExecutor

__all__ = ["BulkDownloadExecutor"]
