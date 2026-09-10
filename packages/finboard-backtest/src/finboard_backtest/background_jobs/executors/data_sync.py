"""``data_sync`` 执行器 —— 全市场标的发现与同步接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data.sync_universe`` 同步阻塞路径。worker 领取
``kind=data_sync`` 任务后,调 :class:`UniverseDiscovery` 发现全市场标的,通过
``InstrumentRepository.sync_with_diff`` 写入 ``instruments`` 表(带生命周期 diff)。

幂等键 = ``sync:{today}``。上游数据源不可用(akshare 缺失 / 网络错误)映射为
``failed(data_source_unavailable)``。``result_ref = None``。

issue #394:指数登记源切换 tushare ``index_basic``(受控 9 只表收窄为基准
资格白名单),``FINBOARD_TUSHARE_TOKEN`` 未配置时具名失败(配置错误不重试);
index_basic ``base_date`` 经 ``backfill_listing_dates`` 回填
``instruments.list_date``(只补 null)。

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
        from finboard_data.research import ResearchDataConfigurationError
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
        except ResearchDataConfigurationError as exc:
            # issue #394:指数登记源已切换 tushare index_basic,token 未配置
            # 是持久配置错误 —— 具名 + 指路,不无限重试。
            raise ExecutorError(
                code="data_source_unavailable",
                summary=(
                    f"标的池同步配置缺失: {exc};请配置 FINBOARD_TUSHARE_TOKEN"
                    "(#394 起指数登记走 tushare index_basic)"
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
            # 后置 enrichment(issue #185;#251 放宽批次口径):akshare 发现链路
            # 不携带 list_date/industry/delist_date,按本次发现范围从最近一次
            # 实际摄取过的 research_instrument_profiles 批次回填(不再要求该批
            # 已发布 —— 此前 profiles 摄取过但从未发布时回填永久短路、元数据全空)。
            backfill = await repo.backfill_metadata_from_profiles(
                symbols=[ins.code for ins in instruments]
            )
            # issue #394:指数登记携带 index_basic base_date,回填
            # instruments.list_date(只补 null,#185/#265 同语义)。profiles
            # 不覆盖指数(那是 stock_basic 档案),两条回填互不干扰。
            from finboard_shared.types import InstrumentType

            listing_backfill = await repo.backfill_listing_dates(
                {
                    ins.code: (ins.list_date, None)
                    for ins in instruments
                    if ins.instrument_type is InstrumentType.INDEX
                    and ins.list_date is not None
                }
            )
            await session.commit()

        # issue #348:完成语收短为 ``data_sync:done`` 短摘要 —— 旧完成语
        # (标的数 + 回填/缺失明细)恒超旧列宽 64 字符,收尾写 phase 触发
        # StringDataRightTruncation,实际已完成的任务被误报 failed。回填
        # 明细数字由下面的 data_sync.done 结构化日志(backfill.as_dict())
        # 完整承载,不丢信息。
        logger.info(
            "data_sync.done",
            **backfill.as_dict(),
            index_list_date_backfilled=listing_backfill["backfilled_list_date"],
            index_missing_list_date=listing_backfill["missing_list_date"],
        )
        await progress(1, 1, "data_sync:done")
        return JobResult(status="succeeded", result_ref=None)


_: type[JobExecutor] = DataSyncExecutor

__all__ = ["DataSyncExecutor"]
