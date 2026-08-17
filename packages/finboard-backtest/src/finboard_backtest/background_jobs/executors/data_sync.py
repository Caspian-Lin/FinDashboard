"""``data_sync`` 执行器 —— 全市场标的发现与同步接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data.sync_universe`` 同步阻塞路径。worker 领取
``kind=data_sync`` 任务后,调 :class:`UniverseDiscovery` 发现全市场标的,通过
``InstrumentRepository.sync_with_diff`` 写入 ``instruments`` 表(带生命周期 diff)。

幂等键 = ``sync:{today}``。上游数据源不可用(akshare 缺失 / 网络错误)映射为
``failed(data_source_unavailable)``。``result_ref = None``。

边界:只写 ``instruments`` 表,不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)

logger = structlog.get_logger(__name__)


class DataSyncExecutor:
    """``kind=data_sync`` 执行器。"""

    KIND = "data_sync"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_maker = session_maker

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        from finboard_data.discovery import UniverseDiscovery
        from finboard_persistence import InstrumentRepository

        await progress(0, None, "data_sync:discovering")
        discovery = UniverseDiscovery()
        try:
            instruments = await discovery.discover_all()
        except ModuleNotFoundError as exc:
            raise ExecutorError(
                code="data_source_unavailable",
                summary=(
                    "标的池同步依赖 akshare,请执行 uv sync --all-packages 后重试"
                ),
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc
        except Exception as exc:
            raise ExecutorError(
                code="data_source_unavailable",
                summary=f"标的池同步失败(上游数据源暂不可用): {exc}",
                retryable=True,
                context={"job_id": job.job_id},
            ) from exc

        dicts: list[dict[str, object]] = [
            {
                "code": ins.code,
                "name": ins.name,
                "market": ins.market.value,
                "instrument_type": ins.instrument_type.value,
                "exchange": ins.exchange,
                "listing_board": ins.listing_board.value,
            }
            for ins in instruments
        ]

        await progress(1, None, "data_sync:syncing")
        async with self._session_maker() as session:
            repo = InstrumentRepository(session)
            await repo.sync_with_diff(dicts, as_of=date.today())
            # 后置 enrichment(issue #185):akshare 发现链路不携带 list_date/
            # industry,按本次发现范围从最近一次已发布的
            # research_instrument_profiles 回填。
            backfill = await repo.backfill_metadata_from_profiles(
                symbols=[ins.code for ins in instruments]
            )
            await session.commit()

        summary = (
            f"标的 {backfill.scoped} 只;回填 list_date={backfill.backfilled_list_date} "
            f"industry={backfill.backfilled_industry};仍缺失 "
            f"list_date={backfill.missing_list_date} industry={backfill.missing_industry}"
            + ("" if backfill.profile_batch_available else "(无已发布档案批次)")
        )
        logger.info("data_sync.done", **backfill.as_dict())
        await progress(1, 1, f"data_sync:done {summary}")
        return JobResult(status="succeeded", result_ref=None)


_: type[JobExecutor] = DataSyncExecutor

__all__ = ["DataSyncExecutor"]
