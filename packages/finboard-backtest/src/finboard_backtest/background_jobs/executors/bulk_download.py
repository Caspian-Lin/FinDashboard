"""``bulk_download`` 执行器 —— 全市场批量历史数据拉取接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data._run_download`` 内存态 asyncio 任务。worker 领取
``kind=bulk_download`` 任务后,从 payload 重建筛选条件与 provider,调
``provider.update_cache_batch`` 更新 Parquet 缓存。

边界:只写本地 ``data_cache`` 缓存目录,不连 broker / 不下实盘单 / 不修改持仓;
单并发由 worker ``kind_concurrency={"bulk_download": 1}`` 保证(避免对上游行情源
造成并发压力)。

suspend_d 停复牌事件(#393):tushare 主源下对股票标的逐标的拉取
``suspend_d`` 事件并幂等落 ``instrument_lifecycle_events``(与 REST / MCP
fetch 的既有 suspension 消费语义一致),失败可见不阻断。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
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
from finboard_backtest.background_jobs.executors._progress import make_sync_progress
from finboard_backtest.background_jobs.executors._providers import (
    build_bar_provider,
    resolve_provider_name,
)
from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
)

if TYPE_CHECKING:
    from finboard_backtest.background_jobs.executors._providers import (
        SettingsFactory,
    )

logger = structlog.get_logger(__name__)

#: 部分失败报告里逐标的列出的上限(#347):error_summary 是单行文本字段,
#: 全市场任务可能有几千个失败标的,超出部分聚合计数(完整清单在 worker 日志
#: ``*.cache_update_failed``),防止超长摘要撑爆任务详情。
_PARTIAL_REPORT_MAX_SYMBOLS = 20

#: suspend_d 停复牌同步的并发窗口(#393):provider 内部信号量 + tushare
#: 请求预算(RPM pacing)仍是真正的限流层,这里只控制批次粒度。
_SUSPEND_CONCURRENCY = 8

#: 停复牌同步的进度上报粒度(phase 文本携带 k/N,数值列保持 bars 口径不变)。
_SUSPEND_PROGRESS_STRIDE = 64


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
        # 入队期契约重放(#260 风格,#347 注册):覆盖旁路入队(直接写库 /
        # 旧版本入队的存量行),未知键 / 非法 source / 坏日期执行期同样 fail-fast。
        try:
            validate_job_payload(job.kind, job.payload)
        except PayloadContractError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=exc.summary,
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc
        market = _require_str(job, "market")
        source_raw = job.payload.get("source")
        if source_raw is not None and not isinstance(source_raw, str):
            raise ExecutorError(
                code="invalid_payload",
                summary="source 必须是字符串",
                retryable=False,
                context={"job_id": job.job_id},
            )
        # REST/MCP 入队对「默认源」写的是空串("" → auto,#341 跟进):交给
        # resolve_provider_name 的回落链(settings 默认 → env → akshare)。
        source = source_raw or None
        start = _parse_date(job, "start")
        # scope 四元组经共享解析重放(#392,#385 口径;与 dataset_sync 同一
        # 函数 —— 大小写归一 / 去重 / 空串丢弃,契约层已验,此处兜底旁路入队)。
        from finboard_backtest.background_jobs.dataset_sync.scope import (
            ScopeValueError,
            normalize_sync_scope,
        )

        try:
            scope = normalize_sync_scope(
                exchange=job.payload.get("exchange"),
                listing_boards=job.payload.get("listing_boards"),
                instrument_type=job.payload.get("instrument_type"),
                symbols=job.payload.get("symbols"),
            )
        except ScopeValueError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=str(exc),
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc
        if job.payload.get("symbols") is not None and not scope.symbols:
            raise ExecutorError(
                code="invalid_payload",
                summary="symbols 不能为空列表(缺省不传 = 全池,#347)",
                retryable=False,
                context={"job_id": job.job_id},
            )
        instrument_type = scope.instrument_type
        exchange = scope.exchange
        listing_boards = list(scope.listing_boards)
        symbols_filter = list(scope.symbols) or None

        from finboard_data.cache import make_symbol
        from finboard_persistence import InstrumentRepository
        from finboard_shared.types import BarPeriod

        await progress(0, None, "bulk_download:resolving_instruments")
        # 1. 从 instruments 表选出活跃标的
        async with self._session_maker() as session:
            repo = InstrumentRepository(session)
            instruments, _ = await repo.list_active(
                market=market,
                instrument_type=instrument_type,
                exchange=exchange,
                listing_boards=listing_boards,
                limit=999999,
            )
            await session.commit()

        # symbols 子集过滤(#347):与 market / instrument_type / exchange /
        # listing_boards 过滤叠加 —— 失败清单可直接回填重跑。交集为空按
        # no_instruments 拒,不让任务静默零迭代。
        if symbols_filter:
            wanted = set(symbols_filter)
            instruments = [
                ins for ins in instruments if getattr(ins, "code", None) in wanted
            ]
            if not instruments:
                raise ExecutorError(
                    code="no_instruments",
                    summary=(
                        "symbols 子集与筛选条件交集为空"
                        "(标的未登记或不在 market/instrument_type 过滤范围内;"
                        "请先同步标的,或放宽过滤条件)"
                    ),
                    retryable=False,
                    context={
                        "job_id": job.job_id,
                        "market": market,
                        "symbols": sorted(wanted)[:_PARTIAL_REPORT_MAX_SYMBOLS],
                    },
                )

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
        # 逐标的失败摘要(#347):provider 侧 on_error 回调聚合异常类型 +
        # 截断消息;返回 dict[code, bool] 里 False 但无回调上报的(如测试
        # stub / 未升级 provider)标注原因未上报,保持缺口可见。
        failures: dict[str, str] = {}

        def _collect_failure(code: str, reason: str) -> None:
            failures.setdefault(code, reason)

        await progress(0, len(sym_objs), "bulk_download:fetching")
        results = await provider.update_cache_batch(
            sym_objs,
            BarPeriod.D1,
            start,
            end,
            on_progress=on_progress,
            on_error=_collect_failure,
        )
        success = sum(results.values())
        failed = len(sym_objs) - success
        # suspend_d 停复牌事件随主源切入缓存侧(#393):tushare 源对股票
        # 标的幂等落 instrument_lifecycle_events;失败可见不阻断(bars 已
        # 成功的部分不回滚,缺口以 error_summary 透出)。
        suspension_summary = await _sync_suspension_events(
            provider,
            provider_name,
            self._session_maker,
            instruments,
            start,
            end,
            progress,
            base_done=len(sym_objs),
        )
        if failed == 0 and suspension_summary is None:
            await progress(len(sym_objs), len(sym_objs), "bulk_download:done")
        else:
            phase = f"bulk_download:partial {failed} failed" if failed else "bulk_download:done"
            await progress(len(sym_objs), len(sym_objs), phase)
        if failed == len(sym_objs) and success == 0:
            raise ExecutorError(
                code="all_symbols_failed",
                summary=_partial_failure_summary(
                    failed, len(sym_objs), results, failures
                ),
                retryable=False,
                context={"job_id": job.job_id, "failed": failed},
            )
        # 「全部失败才 failed」语义不变:部分失败仍 succeeded,但缺口以
        # error_summary 透出(worker 落库路径对该字段不按状态过滤,
        # finboard_job_get / GET /api/jobs/{id} 均可见),phase 同步标注。
        summary_parts = []
        if failed:
            summary_parts.append(
                _partial_failure_summary(failed, len(sym_objs), results, failures)
            )
        if suspension_summary is not None:
            summary_parts.append(suspension_summary)
        return JobResult(
            status="succeeded",
            result_ref=None,
            error_summary=" ;".join(summary_parts) if summary_parts else None,
            progress_total=len(sym_objs),
        )


async def _sync_suspension_events(
    provider: object,
    provider_name: str,
    session_maker: async_sessionmaker[AsyncSession],
    instruments: Sequence[object],
    start: date,
    end: date,
    progress: ProgressCallback,
    *,
    base_done: int,
) -> str | None:
    """tushare 主源下随批量下载同步 suspend_d 停复牌事件(#393)。

    仅对真实 ``TushareBarProvider`` 且为股票的标的生效(指数 / 转债无
    suspend_d 语义;mock provider 静默跳过,保持既有集成测试零感知)。
    逐标的拉取 → 幂等落 ``instrument_lifecycle_events``(与 REST / MCP
    fetch 同一落库函数),失败按标的记录、不阻断任务,返回摘要文本或
    ``None``(全部成功 / 未触发)。进度只写 phase 文本(数值列保持 bars
    口径,避免 done 单调 / total 只增规则被更小的停牌总数卡死)。
    """
    if provider_name != "tushare":
        return None
    from finboard_data import TushareBarProvider

    if not isinstance(provider, TushareBarProvider):
        return None
    stock_codes = [
        str(ins_code)
        for ins_code, ins_type in (
            (getattr(ins, "code", None), getattr(ins, "instrument_type", None))
            for ins in instruments
        )
        if ins_code and ins_type == "stock"
    ]
    if not stock_codes:
        return None

    from finboard_data.cache import make_symbol
    from finboard_persistence import persist_tushare_lifecycle_events

    total = len(stock_codes)
    failures: dict[str, str] = {}
    imported = 0
    await progress(base_done, base_done, f"bulk_download:suspension 0/{total}")

    async def _one(code: str) -> int:
        events = await provider.fetch_suspension_events(make_symbol(code), start, end)
        if not events:
            return 0
        async with session_maker() as session:
            count = await persist_tushare_lifecycle_events(session, events)
            await session.commit()
        return count

    for chunk_start in range(0, total, _SUSPEND_CONCURRENCY):
        chunk = stock_codes[chunk_start : chunk_start + _SUSPEND_CONCURRENCY]
        outcomes = await asyncio.gather(
            *(_one(code) for code in chunk), return_exceptions=True
        )
        for code, outcome in zip(chunk, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                failures.setdefault(code, f"{type(outcome).__name__}: {outcome}"[:200])
            else:
                imported += outcome
        processed = min(chunk_start + len(chunk), total)
        is_last_chunk = processed >= total
        # 首帧已报;中途按步长节流(每次上报是一次 job 行写),收尾帧必达。
        if is_last_chunk or (chunk_start // _SUSPEND_CONCURRENCY) % _SUSPEND_PROGRESS_STRIDE == 0:
            await progress(
                base_done,
                base_done,
                f"bulk_download:suspension {processed}/{total}",
            )
    logger.info(
        "bulk_download.suspension_sync",
        symbols=total,
        imported_events=imported,
        failed_symbols=len(failures),
    )
    if not failures:
        return None
    sample = " ;".join(
        f"{code}: {failures[code]}" for code in sorted(failures)[:5]
    )
    more = (
        f" ;…等共 {len(failures)} 个失败标的" if len(failures) > 5 else ""
    )
    return f"停复牌事件同步失败: {len(failures)}/{total}({sample}{more})"


def _validate_tushare_scope(provider_name: str, instruments: Sequence[object]) -> None:
    """复刻 ``data._validate_bulk_provider_scope``:tushare 批量支持股票/转债/指数。

    可转债(issue #265)与指数(issue #341,2026-09-06 实测 2000 积分档
    可调)各走 ``cb_daily`` / ``index_daily`` 专属接口(TushareBarProvider
    按代码规则分流);期货(issue #267)tushare 侧不接线(fut_daily 属另档
    积分),走 akshare 新浪主连;ETF(issue #341,复权口径对齐未定稿)仍仅
    akshare|yfinance。拒绝行为具名 tushare_scope_mismatch,不静默换源。
    """
    if provider_name != "tushare":
        return
    incompatible = [
        getattr(ins, "code", "?")
        for ins in instruments
        if getattr(ins, "market", None) != "a_share"
        or getattr(ins, "instrument_type", None) not in ("stock", "convertible", "index")
    ]
    if incompatible:
        raise ExecutorError(
            code="tushare_scope_mismatch",
            summary=(
                "Tushare 批量任务支持 A 股股票、可转债与指数;"
                "ETF / 期货请另建任务选 akshare"
            ),
            retryable=False,
            context={"sample": incompatible[:5]},
        )


def _partial_failure_summary(
    failed: int,
    total: int,
    results: dict[str, bool],
    failures: Mapping[str, str],
) -> str:
    """汇总部分失败报告(#347):失败标的 + 异常类型与截断消息。

    排序列出前 :data:`_PARTIAL_REPORT_MAX_SYMBOLS` 个失败标的(排序保证
    报告确定性),超出部分只聚合计数;未收到 on_error 上报的失败标的标注
    原因未上报(测试 stub / 未升级 provider 路径),缺口依旧可见。
    """

    failed_codes = sorted(code for code, ok in results.items() if not ok)
    parts = [
        f"{code}: {failures.get(code, '原因未上报(详见 worker 日志)')}"
        for code in failed_codes[:_PARTIAL_REPORT_MAX_SYMBOLS]
    ]
    listed = " ;".join(parts)
    if len(failed_codes) > _PARTIAL_REPORT_MAX_SYMBOLS:
        listed += (
            f" ;…等共 {len(failed_codes)} 个失败标的"
            "(完整清单见 worker 日志 cache_update_failed)"
        )
    return f"部分标的拉取失败: {failed}/{total}({listed})"


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
