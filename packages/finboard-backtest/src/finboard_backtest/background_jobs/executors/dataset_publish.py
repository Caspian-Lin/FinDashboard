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
from decimal import Decimal

import structlog
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

logger = structlog.get_logger(__name__)

#: 一致性校验差集清单在日志 / 错误摘要里的最大展示条数(总数恒可见)。
_MISMATCH_PREVIEW_LIMIT = 50


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
        kind = _RELEASE_KIND_TO_DATASET_KIND.get(release_kind)
        if kind is None:
            raise ExecutorError(
                code="invalid_payload",
                summary=(
                    "release_kind 必须是 a_share_tushare / multi_asset_mixed / "
                    "daily_metrics / financial_indicators / convertible_metrics"
                ),
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
        # #252:跨发布标的集一致性校验(可选)——对同区间关联发布(如同一批
        # bars / daily_metrics / financial_indicators)做并集差集校验,差集具名。
        # 默认 warning 不阻断;``consistency_fail_on_mismatch=true`` 时秒级失败,
        # 避免标的不一致只在执行期(multi_period 研究运行)才暴露。
        baseline_release_id = job.payload.get("consistency_baseline_release_id")
        if baseline_release_id is not None and not isinstance(baseline_release_id, str):
            raise ExecutorError(
                code="invalid_payload",
                summary="consistency_baseline_release_id 必须是字符串(release_id)",
                retryable=False,
                context={"job_id": job.job_id},
            )
        fail_on_mismatch = job.payload.get("consistency_fail_on_mismatch", False)
        if not isinstance(fail_on_mismatch, bool):
            raise ExecutorError(
                code="invalid_payload",
                summary="consistency_fail_on_mismatch 必须是布尔值",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from finboard_data import (
            DatasetReleaseError,
            DatasetReleaseSpec,
            ImmutableReleaseError,
            ReleaseDatasetKind,
        )
        from finboard_persistence import ResearchDatasetReleaseService
        from finboard_persistence.models import InstrumentModel

        source = _RELEASE_KIND_TO_SOURCE[release_kind]

        await progress(0, None, "dataset_publish:validating")
        mismatch_summary = ""
        async with self._session_maker() as session:
            # #252:与基线发布的标的集 diff(在 scope 校验之前,失败不写任何数据)。
            if baseline_release_id is not None:
                from finboard_persistence import ResearchDatasetReleaseRepository

                baseline = await ResearchDatasetReleaseRepository(session).get(
                    baseline_release_id
                )
                if baseline is None:
                    raise ExecutorError(
                        code="baseline_release_not_found",
                        summary=(
                            f"一致性校验基线发布不存在: {baseline_release_id}"
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )
                baseline_codes = {item.code for item in baseline.instruments}
                release_codes = {str(item) for item in symbols}
                missing_in_release = sorted(baseline_codes - release_codes)
                extra_in_release = sorted(release_codes - baseline_codes)
                if missing_in_release or extra_in_release:
                    mismatch_context = {
                        "job_id": job.job_id,
                        "baseline_release_id": baseline_release_id,
                        "release_id": release_id,
                        "missing_in_release_count": len(missing_in_release),
                        "extra_in_release_count": len(extra_in_release),
                        "missing_in_release": missing_in_release[
                            :_MISMATCH_PREVIEW_LIMIT
                        ],
                        "extra_in_release": extra_in_release[
                            :_MISMATCH_PREVIEW_LIMIT
                        ],
                    }
                    if fail_on_mismatch:
                        raise ExecutorError(
                            code="symbol_set_mismatch",
                            summary=(
                                f"发布 {release_id} 与基线 {baseline_release_id} "
                                f"标的集不一致:基线有本次缺 "
                                f"{len(missing_in_release)} 只,本次有基线缺 "
                                f"{len(extra_in_release)} 只"
                            ),
                            retryable=False,
                            context=mismatch_context,
                        )
                    logger.warning(
                        "dataset_publish.symbol_set_mismatch", **mismatch_context
                    )
                    mismatch_summary = (
                        f" symbol_set_mismatch vs {baseline_release_id}:"
                        f" -{len(missing_in_release)}/+{len(extra_in_release)}"
                    )
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
            if release_kind in ("a_share_tushare", "daily_metrics", "financial_indicators"):
                invalid = sorted(
                    item.code
                    for item in selected
                    if item.market != "a_share" or item.instrument_type != "stock"
                )
                if invalid:
                    raise ExecutorError(
                        code="a_share_scope_violation",
                        summary=(
                            "A股单源发布只能包含 A 股股票: "
                            + ", ".join(invalid[:20])
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )
            elif release_kind == "convertible_metrics":
                # issue #265:转债派生指标发布只接受 A 股可转债。
                invalid = sorted(
                    item.code
                    for item in selected
                    if item.market != "a_share" or item.instrument_type != "convertible"
                )
                if invalid:
                    raise ExecutorError(
                        code="convertible_scope_violation",
                        summary=(
                            "convertible_metrics 发布只能包含 A 股可转债: "
                            + ", ".join(invalid[:20])
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )
            else:
                selected_types = {item.instrument_type for item in selected}
                # issue #184:混合发布放行 index 基准资产(可单独发布指数
                # benchmark 数据集,也可与股票/ETF 混发);issue #265 放行
                # convertible 转债(转债 bars 与正股/基准同处一份发布);
                # issue #267 放行 futures 期货主连(仅基准/研究数据,
                # 不可撮合,对齐 index 先例)。至少含五者之一。
                missing_types = (
                    {"stock", "etf", "index", "convertible", "futures"} - selected_types
                )
                if missing_types:
                    raise ExecutorError(
                        code="mixed_scope_violation",
                        summary=(
                            "多资产混合源发布必须至少包含股票、ETF、指数、可转债或期货主连,"
                            "缺少: "
                            + ", ".join(sorted(missing_types))
                        ),
                        retryable=False,
                        context={"job_id": job.job_id},
                    )

            fields = _default_release_fields(kind)

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
                        fields=fields,
                        dataset_kind=ReleaseDatasetKind(kind),
                        minimum_release_coverage=(
                            # issue #212:研究数据发布级阈值放宽到 0.95——停牌日
                            # 无截面、最新报告期未公告是常态(全市场 daily 实测
                            # 跨度口径平均 coverage≈0.972),0.98 会让任何真实全市场
                            # 研究发布不可发布;0.95 仍拦系统性丢失。逐标的缺口
                            # 已在 builder 侧降级为可见 warning。#265 转债派生
                            # 指标同口径(转债停牌/正股停牌日溢价缺观测)。
                            Decimal("0.95")
                            if release_kind
                            in ("daily_metrics", "financial_indicators", "convertible_metrics")
                            else Decimal("0.98")
                        ),
                        required_capabilities=(
                            ("stock",)
                            if release_kind in ("a_share_tushare", "daily_metrics", "financial_indicators")
                            # issue #265:转债派生指标发布固定要求 convertible 能力。
                            else ("convertible",)
                            if release_kind == "convertible_metrics"
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
                            *(
                                (
                                    "转股溢价率 = 转债收盘 / (100/快照转股价x同日正股收盘) - 1;"
                                    "转股价取 cb_basic 当前快照(下修史不在覆盖范围),"
                                    "非全历史 PIT(#265)"
                                )
                                if release_kind == "convertible_metrics"
                                else ()
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

        await progress(1, 1, f"dataset_publish:done{mismatch_summary}")
        return JobResult(status="succeeded", result_ref=release.release_id)


_RELEASE_KIND_TO_DATASET_KIND: dict[str, str] = {
    "a_share_tushare": "bars",
    "multi_asset_mixed": "bars",
    # issue #187:研究数据发布。daily_metrics / financial_indicators 由
    # research_data_sync(#171)摄取进 research_* 表,发布从表冻结而非本地缓存。
    "daily_metrics": "daily_metrics",
    "financial_indicators": "financial_indicators",
    # issue #265:可转债派生指标发布(转股价值/转股溢价率),发布执行时从
    # 本地缓存 bars x 冻结转股价元数据计算。
    "convertible_metrics": "convertible_metrics",
}

_RELEASE_KIND_TO_SOURCE: dict[str, str] = {
    "a_share_tushare": "tushare",
    "multi_asset_mixed": "mixed",
    "daily_metrics": "tushare",
    "financial_indicators": "tushare",
    "convertible_metrics": "tushare",
}


def _default_release_fields(kind_value: str) -> tuple[str, ...]:
    """按数据集类型返回默认冻结字段白名单(全部字段)。"""
    from finboard_data import (
        CONVERTIBLE_METRICS_FIELDS,
        DAILY_METRICS_FIELDS,
        FINANCIAL_INDICATORS_FIELDS,
        RELEASE_FIELDS,
    )

    if kind_value == "daily_metrics":
        return DAILY_METRICS_FIELDS
    if kind_value == "financial_indicators":
        return FINANCIAL_INDICATORS_FIELDS
    if kind_value == "convertible_metrics":
        return CONVERTIBLE_METRICS_FIELDS
    return RELEASE_FIELDS


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
