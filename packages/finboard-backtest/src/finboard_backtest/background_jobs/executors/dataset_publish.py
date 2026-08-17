"""``dataset_publish`` 执行器 —— 冻结发布接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.instruments.create_dataset_release`` 同步阻塞路径。
worker 领取 ``kind=dataset_publish`` 任务后,从 payload 重建
:class:`DatasetReleaseSpec`(含标的资产类型校验),调
``ResearchDatasetReleaseService.publish`` 完成原子 rename + DB 登记。

幂等键 = ``publish:{release_id}``(release_id 天然唯一),重复提交同一 release_id
不会重复发布。``ImmutableReleaseError`` 映射为不可重试失败(release_id 身份冲突)。

边界:只写 ``research_dataset_releases`` 表与 ``release_root`` 目录,不连 broker /
不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors._runtime import (
    cache_dir,
    code_version,
    release_root,
)


class DatasetPublishExecutor:
    """``kind=dataset_publish`` 执行器。"""

    KIND = "dataset_publish"

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
        release_id = _require_str(job, "release_id")
        dataset_name = _require_str(job, "dataset_name")
        release_kind = _require_str(job, "release_kind")
        if release_kind not in ("a_share_tushare", "multi_asset_mixed"):
            raise ExecutorError(
                code="invalid_payload",
                summary="release_kind 必须是 a_share_tushare 或 multi_asset_mixed",
                retryable=False,
                context={"job_id": job.job_id},
            )
        version = _require_str(job, "version")
        start_date = _parse_date(job, "start_date")
        end_date = _parse_date(job, "end_date")
        adjustment = _require_str(job, "adjustment")
        symbols = job.payload.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ExecutorError(
                code="invalid_payload",
                summary="symbols 必须是非空字符串列表",
                retryable=False,
                context={"job_id": job.job_id},
            )
        required_capabilities = job.payload.get("required_capabilities", [])
        if not isinstance(required_capabilities, list):
            raise ExecutorError(
                code="invalid_payload",
                summary="required_capabilities 必须是列表",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from finboard_data import (
            DatasetReleaseError,
            DatasetReleaseSpec,
            ImmutableReleaseError,
        )
        from finboard_persistence import ResearchDatasetReleaseService
        from finboard_persistence.models import InstrumentModel

        source = (
            "tushare"
            if release_kind == "a_share_tushare"
            else "mixed"
        )

        await progress(0, None, "dataset_publish:validating")
        async with self._session_maker() as session:
            rows = await session.execute(
                select(InstrumentModel).where(InstrumentModel.code.in_(symbols))
            )
            selected = list(rows.scalars().all())
            selected_codes = {item.code for item in selected}
            missing = sorted(set(symbols) - selected_codes)
            if missing:
                raise ExecutorError(
                    code="unknown_symbols",
                    summary=f"发布标的未登记到元数据表: {', '.join(missing[:20])}",
                    retryable=False,
                    context={"job_id": job.job_id},
                )
            if release_kind == "a_share_tushare":
                invalid = sorted(
                    item.code
                    for item in selected
                    if item.market != "a_share" or item.instrument_type != "stock"
                )
                if invalid:
                    raise ExecutorError(
                        code="a_share_scope_violation",
                        summary=(
                            "A股 Tushare 单源发布只能包含 A 股股票: "
                            + ", ".join(invalid[:20])
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )
            else:
                selected_types = {item.instrument_type for item in selected}
                # issue #184:混合发布放行 index 基准资产(可单独发布指数
                # benchmark 数据集,也可与股票/ETF 混发);至少含三者之一。
                missing_types = {"stock", "etf", "index"} - selected_types
                if missing_types:
                    raise ExecutorError(
                        code="mixed_scope_violation",
                        summary=(
                            "多资产混合源发布必须至少包含股票、ETF 或指数,缺少: "
                            + ", ".join(sorted(missing_types))
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )

            await progress(1, None, "dataset_publish:publishing")
            service = ResearchDatasetReleaseService(
                session,
                cache_dir=cache_dir(),
                release_root=release_root(),
            )
            try:
                release = await service.publish(
                    DatasetReleaseSpec(
                        release_id=release_id,
                        dataset_name=dataset_name,
                        source=source,
                        version=version,
                        start_date=start_date,
                        end_date=end_date,
                        code_version=code_version(),
                        adjustment=adjustment,
                        required_capabilities=(
                            ("stock",)
                            if release_kind == "a_share_tushare"
                            else tuple(required_capabilities)
                        ),
                        known_limitations=(
                            "交易日覆盖使用 akshare/exchange_calendars 真实 A 股交易日历",
                            "停牌优先使用停复牌生命周期事件;缺少事件时按本地缓存的已查询区间(covered_ranges)对齐批量拉取口径",
                            "只冻结本地缓存已有字段,不会回退到联网数据源",
                            (
                                "A股单源发布严格要求所有 Bar 来源为 tushare"
                                if release_kind == "a_share_tushare"
                                else "多资产发布允许按标的混合来源,实际来源写入质量报告"
                            ),
                        ),
                    ),
                    symbols,
                )
                await session.commit()
            except ImmutableReleaseError as exc:
                await session.rollback()
                raise ExecutorError(
                    code="release_identity_conflict",
                    summary=f"发布身份冲突: {exc}",
                    retryable=False,
                    context={"job_id": job.job_id, "release_id": release_id},
                ) from exc
            except (DatasetReleaseError, ValueError) as exc:
                await session.rollback()
                raise ExecutorError(
                    code="quality_gate_failed",
                    summary=f"数据质量门未通过: {exc}",
                    retryable=False,
                    context={"job_id": job.job_id},
                ) from exc

        await progress(1, 1, "dataset_publish:done")
        return JobResult(status="succeeded", result_ref=release.release_id)


def _require_str(job: JobRecord, key: str) -> str:
    raw = job.payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"dataset_publish 任务 payload 必须包含合法 {key}",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


def _parse_date(job: JobRecord, key: str) -> date:
    raw = job.payload.get(key)
    if not isinstance(raw, str):
        raise ExecutorError(
            code="invalid_payload",
            summary=f"dataset_publish 任务 payload 缺少 {key}",
            retryable=False,
            context={"job_id": job.job_id},
        )
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"dataset_publish 任务 payload {key} 不是合法日期",
            retryable=False,
            context={"job_id": job.job_id},
        ) from exc


_: type[JobExecutor] = DatasetPublishExecutor

__all__: list[str] = ["DatasetPublishExecutor"]
