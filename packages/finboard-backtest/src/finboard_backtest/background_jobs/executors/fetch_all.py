"""``fetch_all`` 执行器 —— 标的池批量缓存更新接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data.fetch_all_data`` 同步阻塞路径。worker 领取
``kind=fetch_all`` 任务后,读取标的池配置,调 ``provider.update_cache_batch``
更新本地 Parquet 缓存(不在内存中保留所有历史 Bars)。

与 ``bulk_download`` 共用底层 ``update_cache_batch``;区别是 fetch_all 的标的来自
``symbols.yaml`` 配置池,而 bulk_download 按 market/instrument_type 从 DB 筛选。

幂等键 = ``fetch_all:{symbol_pool+lookback}``。``result_ref = None``。

边界:只写本地 ``data_cache`` 缓存目录,不连 broker / 不下实盘单 / 不修改持仓;
单并发由 ``kind_concurrency={"fetch_all": 1}`` 保证。
"""

from __future__ import annotations

from datetime import date, timedelta
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
    from finboard_backtest.background_jobs.executors._providers import SettingsFactory

_SYMBOLS_FILE = "symbols.yaml"


class DataFetchAllExecutor:
    """``kind=fetch_all`` 执行器。"""

    KIND = "fetch_all"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: SettingsFactory,
        symbols_file: str = _SYMBOLS_FILE,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory
        self._symbols_file = symbols_file

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        from finboard_data import load_symbol_pool
        from finboard_data.cache import make_symbol
        from finboard_shared.types import BarPeriod

        config = load_symbol_pool(self._symbols_file)
        if not config.symbols:
            await progress(1, 1, "fetch_all:empty_pool")
            return JobResult(status="succeeded", result_ref=None, progress_total=0)

        end = date.today()
        lookback = job.payload.get("lookback_days")
        if lookback is not None and not isinstance(lookback, int):
            raise ExecutorError(
                code="invalid_payload",
                summary="lookback_days 必须是整数",
                retryable=False,
                context={"job_id": job.job_id},
            )
        start = end - timedelta(days=lookback if lookback else config.fetch_lookback_days)
        period = (
            BarPeriod[config.fetch_period]
            if config.fetch_period in BarPeriod.__members__
            else BarPeriod(config.fetch_period)
        )
        provider_name = resolve_provider_name(None, self._settings_factory)
        provider = build_bar_provider(provider_name, self._settings_factory)
        sym_objs = [make_symbol(s.code) for s in config.symbols]

        total_holder: dict[str, int | None] = {"total": len(sym_objs)}
        on_progress = make_sync_progress(
            progress, phase_prefix="fetch_all", total_holder=total_holder
        )
        await progress(0, len(sym_objs), "fetch_all:fetching")
        await provider.update_cache_batch(
            sym_objs,
            period,
            start,
            end,
            adjust=config.fetch_adjust,
            on_progress=on_progress,
        )
        await progress(len(sym_objs), len(sym_objs), "fetch_all:done")
        return JobResult(
            status="succeeded",
            result_ref=None,
            progress_total=len(sym_objs),
        )


_: type[JobExecutor] = DataFetchAllExecutor

__all__ = ["DataFetchAllExecutor"]
