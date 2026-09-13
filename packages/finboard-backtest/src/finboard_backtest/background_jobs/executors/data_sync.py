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

issue #395:期货合约级登记(tushare ``fut_basic``,CFFEX 股指四品种在市
合约)并入发现;合约 ``list_date`` / ``delist_date`` 与指数 base_date 同走
``backfill_listing_dates`` 只补 null 通道。期货交易日历(``fut_trade_cal``,
CFFEX 行集,含休市行)幂等落库 ``trade_cal`` 表(与 #396 股票 trade_cal
同表同构,exchange 区分);日历同步**尽力而为**:token 缺失 / 上游失败
具名告警不阻断标的同步(日历是附加参考数据,标的同步是任务本体)。

边界:只写 ``instruments`` / ``trade_cal`` 表,不连 broker / 不下实盘单 /
不修改持仓。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)

if TYPE_CHECKING:
    from finboard_data.research import FuturesTradeCalendarDay

logger = structlog.get_logger(__name__)

#: 期货交易日历回溯窗口起点(#395):与 bulk_download 默认 start(2015-01-01)
#: 对齐 —— 研究窗口默认覆盖 2015 年起的回测区间。
_FUT_TRADE_CAL_START = date(2015, 1, 1)

#: 期货交易日历登记交易所(#395):CFFEX 股指期货(登记域同品种口径,
#: #267);其他交易所日历扩域 = 加一行(上游接口同构)。
_FUT_TRADE_CAL_EXCHANGE = "CFFEX"


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

        # 期货交易日历(#395,尽力而为):网络摄取在开事务前完成,不长时间
        # 占用 DB 会话;token 缺失 / 上游失败具名告警不阻断标的同步。
        calendar_days = await _fetch_futures_trade_calendar(job_id=job.job_id)

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
            # 上市/退市日结构化回填(只补 null,#185/#265/#394/#395 同语义):
            # 指数 = index_basic base_date(#394);期货合约 = fut_basic
            # list_date / delist_date(#395,与转债 cb_basic 回填同通道)。
            from finboard_shared.types import InstrumentType

            listing_records: dict[str, tuple[date | None, date | None]] = {}
            for ins in instruments:
                if ins.instrument_type is InstrumentType.INDEX:
                    if ins.list_date is not None:
                        listing_records[ins.code] = (ins.list_date, None)
                elif (
                    ins.instrument_type is InstrumentType.FUTURES
                    and (ins.list_date is not None or ins.delist_date is not None)
                ):
                    listing_records[ins.code] = (ins.list_date, ins.delist_date)
            listing_backfill = await repo.backfill_listing_dates(listing_records)
            # 日历幂等 upsert(与 #396 股票 trade_cal 同表同构,exchange 区分)。
            calendar_summary: dict[str, int] | None = None
            if calendar_days is not None:
                from finboard_persistence import TradeCalRepository

                calendar_summary = await TradeCalRepository(
                    session
                ).upsert_calendar_days(calendar_days, source="tushare")

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
            futures_listing_backfilled=listing_backfill["backfilled_delist_date"],
            futures_trade_cal_trading_days=(
                calendar_summary["trading_days"] if calendar_summary else None
            ),
        )
        await progress(1, 1, "data_sync:done")
        return JobResult(status="succeeded", result_ref=None)


async def _fetch_futures_trade_calendar(
    *, job_id: str
) -> list[FuturesTradeCalendarDay] | None:
    """拉取期货交易日历(#395,尽力而为):失败具名告警返回 None。

    窗口 = 2015-01-01 起(与 bulk_download 默认 start 对齐)至明年年末
    (跨年排程可见);交易所 CFFEX(登记域同品种口径,#267)。token 未
    配置 / 上游失败都不阻断 data_sync 主流程 —— 交易日历是附加参考数据,
    标的同步是任务本体;缺口以结构化日志可见。
    """
    from finboard_data.research import ResearchDataConfigurationError
    from finboard_data.tushare_provider import TushareResearchDataProvider

    end_year = date.today().year + 1
    window_end = date(end_year, 12, 31)
    try:
        provider = TushareResearchDataProvider()
        days = await provider.fetch_futures_trade_cal(
            exchange=_FUT_TRADE_CAL_EXCHANGE,
            start_date=_FUT_TRADE_CAL_START,
            end_date=window_end,
        )
    except ResearchDataConfigurationError as exc:
        logger.warning(
            "data_sync.futures_trade_cal_skipped",
            job_id=job_id,
            reason="tushare_token_missing",
            detail=str(exc),
        )
        return None
    except Exception as exc:
        logger.warning(
            "data_sync.futures_trade_cal_failed",
            job_id=job_id,
            exchange=_FUT_TRADE_CAL_EXCHANGE,
            detail=str(exc),
        )
        return None
    logger.info(
        "data_sync.futures_trade_cal_fetched",
        job_id=job_id,
        exchange=_FUT_TRADE_CAL_EXCHANGE,
        days=len(days),
        window=f"{_FUT_TRADE_CAL_START.isoformat()}..{window_end.isoformat()}",
    )
    return days


_: type[JobExecutor] = DataSyncExecutor

__all__ = ["DataSyncExecutor"]
