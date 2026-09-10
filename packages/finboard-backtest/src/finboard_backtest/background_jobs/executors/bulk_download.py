"""``bulk_download`` 执行器 —— 全市场批量历史数据拉取接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.data._run_download`` 内存态 asyncio 任务。worker 领取
``kind=bulk_download`` 任务后,从 payload 重建筛选条件与 provider,调
``provider.update_cache_batch`` 更新 Parquet 缓存。

边界:只写本地 ``data_cache`` 缓存目录,不连 broker / 不下实盘单 / 不修改持仓;
单并发由 worker ``kind_concurrency={"bulk_download": 1}`` 保证(避免对上游行情源
造成并发压力)。
"""

from __future__ import annotations

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

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from finboard_backtest.background_jobs.executors._providers import (
        SettingsFactory,
    )

#: 部分失败报告里逐标的列出的上限(#347):error_summary 是单行文本字段,
#: 全市场任务可能有几千个失败标的,超出部分聚合计数(完整清单在 worker 日志
#: ``*.cache_update_failed``),防止超长摘要撑爆任务详情。
_PARTIAL_REPORT_MAX_SYMBOLS = 20


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

        provider_name = _resolve_bulk_provider_name(
            source, instruments, self._settings_factory, job_id=job.job_id
        )
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
        if failed == 0:
            await progress(len(sym_objs), len(sym_objs), "bulk_download:done")
        else:
            await progress(
                len(sym_objs),
                len(sym_objs),
                f"bulk_download:partial {failed} failed",
            )
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
        return JobResult(
            status="succeeded",
            result_ref=None,
            error_summary=(
                _partial_failure_summary(failed, len(sym_objs), results, failures)
                if failed
                else None
            ),
            progress_total=len(sym_objs),
        )


def _resolve_bulk_provider_name(
    source: str | None,
    instruments: Sequence[object],
    settings_factory: SettingsFactory,
    *,
    job_id: str,
) -> str:
    """解析批量任务行情源,叠加指数主源偏好(issue #394)。

    入队 payload **显式声明** ``source`` 时一律照旧(显式选择恒优先);
    未声明(REST/MCP 默认写空串)且筛选域**全部为指数**时,默认源覆盖为
    tushare —— index_daily 是指数日线的结构化主源(原始点位,无复权
    概念,#341 实测 2000 积分档可调),akshare ``index_zh_a_hist`` 降为
    副源(可显式 ``source=akshare`` 选回)。混合域(含股票 / ETF)不
    覆盖 —— 全局默认源的切换归 ``settings.data_provider`` 管(#404),
    本偏好只对指数专属任务生效,避免改动股票复权口径链路。
    """
    provider_name = resolve_provider_name(source, settings_factory)
    if source is not None or provider_name == "tushare" or not instruments:
        return provider_name
    if all(
        getattr(ins, "instrument_type", None) == "index" for ins in instruments
    ):
        logger.info(
            "bulk_download.index_source_preference",
            job_id=job_id,
            default_provider=provider_name,
            resolved_provider="tushare",
            symbols=len(instruments),
            message="指数专属批量任务默认走 tushare index_daily(#394 主源);显式 source 可覆盖",
        )
        return "tushare"
    return provider_name


def _validate_tushare_scope(provider_name: str, instruments: Sequence[object]) -> None:
    """复刻 ``data._validate_bulk_provider_scope``:tushare 批量支持股票/转债/指数/期货。

    可转债(issue #265)、指数(issue #341,2026-09-06 实测 2000 积分档
    可调)与期货(issue #395,拍板更新 #267「fut_daily 属另档积分」旧记录)
    各走 ``cb_daily`` / ``index_daily`` / ``fut_daily`` 专属接口
    (TushareBarProvider 按代码规则分流);ETF(issue #341,复权口径对齐
    未定稿)仍仅 akshare|yfinance。拒绝行为具名 tushare_scope_mismatch,
    不静默换源。
    """
    if provider_name != "tushare":
        return
    incompatible: list[str] = []
    for ins in instruments:
        market = getattr(ins, "market", None)
        instrument_type = getattr(ins, "instrument_type", None)
        scope_ok = (
            market == "a_share"
            and instrument_type in ("stock", "convertible", "index")
        ) or (market == "future" and instrument_type == "futures")
        if not scope_ok:
            incompatible.append(getattr(ins, "code", "?"))
    if incompatible:
        raise ExecutorError(
            code="tushare_scope_mismatch",
            summary=(
                "Tushare 批量任务支持 A 股股票、可转债、指数与期货;"
                "ETF 请另建任务选 akshare"
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
