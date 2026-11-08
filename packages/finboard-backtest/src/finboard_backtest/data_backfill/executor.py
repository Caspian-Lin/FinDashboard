"""``data_backfill`` 执行器 —— 补数请求编排(issue #511,方案 5.4)。

本执行器**不复制任何同步/发布逻辑**:按明细的数据集类型组合既有四个执行器:

* ``dataset_sync`` —— 研究表 / 主数据 / 事件数据集的摄取(批次记账、行级口径、
  预算共享全部复用 #392 框架);
* ``bulk_download`` —— bars 缓存同步;``quality_repair`` 仅在预检标了
  ``quality_repair`` 或 ``action=repair`` 时执行;
* ``dataset_publish`` —— 冻结新发布(质量门、不可变身份、分段事务全部复用
  #144 执行器)。补数**只产生新发布**,既有发布只读。

执行语义:

* **逐 item 原子领取**:``UPDATE ... WHERE status IN ('pending','failed')
  RETURNING``;重复执行时已 ``succeeded`` / 被同请求其他执行器领走的 item 跳过;
* **默认 fail-fast**:第一个 item 失败即停止后续 item(已完成 item 的发布保留,
  可被其他课题选用);请求 ``failed`` 或 ``partial``(有成功 item 时);
* **发布幂等**:release_id = ``DR-<dataset>-<request_id>``,version =
  ``v-<request_id>``;重跑命中同一发布身份时直接复用,不重复冻结;
* **预算耗尽**:底层 ``TushareRequestLimitError`` → ``retry_waiting``
  (与 ``dataset_sync`` 同语义),请求退回 ``queued``、item 退回 ``pending``,
  不把请求标死;
* **取消**:worker 的协作式取消经 ``progress`` 回调抛 ``CancelledError``;
  这里先把请求/未完成 item 收敛为 ``cancelled`` 再原样上抛(已发布不回滚)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
    truncate_summary,
)
from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
)
from finboard_backtest.data_backfill.contracts import (
    BackfillAction,
    BackfillItemStatus,
    BackfillRequestStatus,
)
from finboard_backtest.data_backfill.support import (
    BackfillSupport,
    support_for,
)
from finboard_persistence.catalog_repo import CatalogRepository
from finboard_persistence.data_backfill_repo import DataBackfillRepository
from finboard_persistence.models import (
    DataBackfillRequestItemModel,
    InstrumentModel,
    ResearchSyncBatchModel,
)

logger = structlog.get_logger(__name__)

#: 发布身份前缀(与 ``research_dataset_releases`` 的 ``DR-`` 惯例一致)。
_RELEASE_ID_PREFIX = "DR-"

#: 质量失败的批次状态(``dataset_sync`` 质量门拒绝)。
_QUALITY_FAILED_BATCH_STATUSES: frozenset[str] = frozenset(
    {"quality_rejected"}
)

#: 质量失败的批次质量码。
_QUALITY_FAILED_STATUSES: frozenset[str] = frozenset({"failed", "rejected"})

#: bars 发布允许的资产类型(与 ``DatasetPublishExecutor`` 的
#: ``multi_asset_mixed`` 白名单一致:股票 / ETF / 指数 / 转债 / 期货主连)。
_BARS_INSTRUMENT_TYPES: tuple[str, ...] = (
    "stock",
    "etf",
    "index",
    "convertible",
    "futures",
)


class DataBackfillExecutor:
    """``kind=data_backfill`` 执行器:按冻结的预检快照编排补数。"""

    KIND = "data_backfill"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings_factory: Callable[[], Any] | None = None,
        executor_factory: Callable[[str], JobExecutor] | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._settings_factory = settings_factory
        self._executor_factory = executor_factory or self._default_executor

    # ------------------------------------------------------------------ public
    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        try:
            validate_job_payload(job.kind, job.payload)
        except PayloadContractError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=exc.summary,
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc
        request_id = _require_request_id(job)

        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            request = await repo.get(request_id)
            if request is None:
                raise ExecutorError(
                    code="backfill_request_not_found",
                    summary=f"补数请求不存在: {request_id}",
                    retryable=False,
                    context={"job_id": job.job_id},
                )
            if request.status in (
                BackfillRequestStatus.SUCCEEDED.value,
                BackfillRequestStatus.BLOCKED.value,
            ):
                # 幂等重放:请求已收敛,不重复消耗上游配额。
                await session.commit()
                return JobResult(
                    status="succeeded", result_ref=request.request_id
                )
            if request.status == BackfillRequestStatus.CANCELLED.value:
                await session.commit()
                return JobResult(status="cancelled")
            await repo.set_request_status(
                request_id,
                status=BackfillRequestStatus.RUNNING.value,
                started=True,
            )
            items = await repo.list_items(request_id)
            await session.commit()

        executable = [
            item
            for item in items
            if item.action
            in (
                BackfillAction.SYNC.value,
                BackfillAction.SYNC_PUBLISH.value,
                BackfillAction.REPAIR.value,
            )
            and item.status
            in (
                BackfillItemStatus.PENDING.value,
                BackfillItemStatus.FAILED.value,
            )
        ]
        total = len(executable)
        try:
            for index, item in enumerate(executable, start=1):
                claimed = await self._claim(item.id)
                if claimed is None:
                    # 已被同请求的其他执行器领走 / 已成功:跳过,不重复消耗配额。
                    continue
                await progress(index - 1, total, f"data_backfill:{item.dataset}")
                await self._run_item(
                    claimed, job=job, progress=progress, index=index, total=total
                )
            _status, error_code, error_summary = await self._finalize(request_id)
            await progress(total, total, "data_backfill:done")
            return JobResult(
                status="succeeded",
                result_ref=request_id,
                error_code=error_code,
                error_summary=error_summary,
                progress_total=total,
            )
        except asyncio.CancelledError:
            # 协作式取消:先收敛请求与未完成明细,再把取消原样交给 worker。
            await self._mark_cancelled(request_id)
            logger.info("data_backfill.cancelled", request_id=request_id)
            raise
        except ExecutorError as exc:
            await self._fail(request_id, exc)
            logger.warning(
                "data_backfill.failed",
                request_id=request_id,
                code=exc.code,
                summary=exc.summary,
            )
            raise
        except Exception as exc:  # pragma: no cover - 兜底为不可重试失败
            wrapped = ExecutorError(
                code=type(exc).__name__,
                summary=str(exc)[:1000] or type(exc).__name__,
                retryable=False,
                context={"job_id": job.job_id, "request_id": request_id},
            )
            await self._fail(request_id, wrapped)
            raise wrapped from exc

    # ------------------------------------------------------------------ items
    async def _run_item(
        self,
        item: DataBackfillRequestItemModel,
        *,
        job: JobRecord,
        progress: ProgressCallback,
        index: int,
        total: int,
    ) -> None:
        support = support_for(item.dataset)
        child = _child_progress(progress, item=item, index=index, total=total)
        try:
            batch_ids = await self._sync_phase(item, support, job=job, progress=child)
            await self._update_item(
                item.id,
                status=BackfillItemStatus.SUCCEEDED.value,
                batch_ids=batch_ids,
            )
            if item.action in (
                BackfillAction.SYNC_PUBLISH.value,
                BackfillAction.REPAIR.value,
            ):
                release_id = await self._publish_phase(item, support)
                await self._update_item(
                    item.id,
                    status=BackfillItemStatus.SUCCEEDED.value,
                    batch_ids=batch_ids,
                    release_id=release_id,
                )
                async with self._session_maker() as session:
                    await DataBackfillRepository(session).add_release_id(
                        item.request_id, release_id
                    )
                    await session.commit()
        except ExecutorError:
            await self._update_item(
                item.id,
                status=BackfillItemStatus.FAILED.value,
            )
            raise

    async def _sync_phase(
        self,
        item: DataBackfillRequestItemModel,
        support: BackfillSupport,
        *,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> list[int]:
        """按支持清单选择同步内核;返回产出批次 ID(发布/审计用)。"""

        if not support.syncable:
            # 派生数据集(convertible_metrics)不单独同步,直接进入发布阶段。
            return []
        if support.sync_mode == "bulk_download":
            await self._run_child(
                "bulk_download",
                JobRecord(
                    job_id=job.job_id,
                    kind="bulk_download",
                    queue=job.queue,
                    payload={
                        "market": await self._resolve_market(item),
                        "source": item.source,
                        "start": item.start_date.isoformat(),
                        "adjust": item.adjustment,
                        **(
                            {"symbols": list(item.symbols)}
                            if item.symbols
                            else {}
                        ),
                    },
                    attempt=job.attempt,
                    max_attempts=job.max_attempts,
                    requested_by=job.requested_by,
                ),
                progress,
            )
            if item.action == BackfillAction.REPAIR.value:
                await self._run_child(
                    "quality_repair",
                    JobRecord(
                        job_id=job.job_id,
                        kind="quality_repair",
                        queue=job.queue,
                        payload={
                            "symbols": list(item.symbols),
                            "source": item.source,
                            "adjust": item.adjustment,
                        },
                        attempt=job.attempt,
                        max_attempts=job.max_attempts,
                        requested_by=job.requested_by,
                    ),
                    progress,
                )
            return []
        max_batch_id = await self._max_batch_id(support.sync_dataset)
        await self._run_child(
            "dataset_sync",
            JobRecord(
                job_id=job.job_id,
                kind="dataset_sync",
                queue=job.queue,
                payload={
                    "datasets": [support.sync_dataset],
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                    **({"symbols": list(item.symbols)} if item.symbols else {}),
                },
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                requested_by=job.requested_by,
            ),
            progress,
        )
        batch_ids = await self._batch_ids_after(
            support.sync_dataset, after_id=max_batch_id
        )
        await self._assert_batch_quality(support.sync_dataset, batch_ids)
        return batch_ids

    async def _publish_phase(
        self, item: DataBackfillRequestItemModel, support: BackfillSupport
    ) -> str:
        """冻结新发布(身份由 request_id 确定性决定,重跑幂等)."""

        if not support.publishable or support.release_kind is None:
            raise ExecutorError(
                code="backfill_unsupported_dataset",
                summary=f"{item.dataset} 不支持发布",
                retryable=False,
            )
        release_id = release_id_for(item.dataset, item.request_id)
        version = version_for(item.request_id)
        if item.release_id:
            existing = await self._release_exists(item.release_id)
            if existing:
                return item.release_id
        symbols = list(item.symbols) or await self._publish_symbols(item.dataset)
        if not symbols:
            raise ExecutorError(
                code="backfill_empty_symbol_pool",
                summary=(
                    f"{item.dataset} 发布标的为空(元数据表没有匹配标的);"
                    "请先同步标的或缩小范围"
                ),
                retryable=False,
            )
        await self._run_child(
            "dataset_publish",
            JobRecord(
                job_id=f"{item.request_id}:{item.dataset}",
                kind="dataset_publish",
                queue="data",
                payload={
                    "release_id": release_id,
                    "dataset_name": item.dataset,
                    "release_kind": support.release_kind,
                    "version": version,
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                    "adjustment": item.adjustment,
                    "symbols": symbols,
                },
                attempt=1,
                max_attempts=1,
                requested_by="data_backfill",
            ),
            _noop_progress,
        )
        return release_id

    async def _run_child(
        self,
        kind: str,
        child_job: JobRecord,
        progress: ProgressCallback,
    ) -> None:
        """调用既有执行器并把预算耗尽 / 质量失败映射为补数语义。"""

        from finboard_data.tushare_budget import TushareRequestLimitError

        executor = self._executor_factory(kind)
        try:
            result = await executor.execute(child_job, progress)
        except TushareRequestLimitError as exc:
            raise ExecutorError(
                code="backfill_quota_insufficient",
                summary=f"上游请求预算耗尽,任务将退避重试: {exc}",
                retryable=True,
                context={"request_id": child_job.payload.get("request_id")},
            ) from exc
        except ExecutorError as exc:
            if exc.code in ("tushare_budget_exhausted",):
                raise ExecutorError(
                    code="backfill_quota_insufficient",
                    summary=exc.summary,
                    retryable=True,
                    context=exc.context,
                ) from exc
            if exc.code in ("quality_gate_failed",):
                raise ExecutorError(
                    code="backfill_quality_failed",
                    summary=exc.summary,
                    retryable=False,
                    context=exc.context,
                ) from exc
            if exc.code in ("release_identity_conflict",):
                raise ExecutorError(
                    code="backfill_publish_conflict",
                    summary=exc.summary,
                    retryable=False,
                    context=exc.context,
                ) from exc
            raise
        if result.status not in ("succeeded",):
            raise ExecutorError(
                code=f"backfill_{kind}_not_finished",
                summary=(
                    f"{kind} 子任务未成功(status={result.status},"
                    f" error={result.error_summary or result.error_code})"
                ),
                retryable=result.status in ("retry_waiting", "interrupted"),
                context={"dataset": child_job.payload.get("datasets")},
            )

    # ------------------------------------------------------------- persistence
    async def _claim(self, item_id: int) -> DataBackfillRequestItemModel | None:
        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            claimed = await repo.claim_item(item_id)
            await session.commit()
            return claimed

    async def _update_item(
        self, item_id: int, *, status: str, **values: Any
    ) -> None:
        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            await repo.update_item(item_id, status=status, **values)
            await session.commit()

    async def _finalize(self, request_id: str) -> tuple[str, str | None, str | None]:
        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            request = await repo.get(request_id)
            if request is None:
                return ("failed", "backfill_request_not_found", "请求行已消失")
            items = await repo.list_items(request_id)
            failed = [
                item
                for item in items
                if item.status == BackfillItemStatus.FAILED.value
            ]
            succeeded = [
                item
                for item in items
                if item.status == BackfillItemStatus.SUCCEEDED.value
            ]
            if failed:
                status = (
                    BackfillRequestStatus.PARTIAL.value
                    if succeeded
                    else BackfillRequestStatus.FAILED.value
                )
                summary = "; ".join(
                    f"{item.dataset}: {item.error_code or 'failed'}" for item in failed
                )[:1000]
                await repo.set_request_status(
                    request_id,
                    status=status,
                    error_code="backfill_item_failed",
                    error_summary=truncate_summary(summary),
                    finished=True,
                )
                await session.commit()
                return (status, "backfill_item_failed", summary)
            await repo.set_request_status(
                request_id,
                status=BackfillRequestStatus.SUCCEEDED.value,
                finished=True,
            )
            await session.commit()
            return ("succeeded", None, None)

    async def _fail(self, request_id: str, error: ExecutorError) -> None:
        """失败收口:可重试错误退回 queued(不把请求标死),其余终态失败。"""

        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            if error.retryable:
                await repo.set_request_status(
                    request_id,
                    status=BackfillRequestStatus.QUEUED.value,
                    error_code=error.code,
                    error_summary=truncate_summary(error.summary),
                )
                await self._reset_running_items(repo, request_id)
            else:
                await repo.set_request_status(
                    request_id,
                    status=BackfillRequestStatus.FAILED.value,
                    error_code=error.code,
                    error_summary=truncate_summary(error.summary),
                    finished=True,
                )
            await session.commit()

    async def _mark_cancelled(self, request_id: str) -> None:
        async with self._session_maker() as session:
            repo = DataBackfillRepository(session)
            await repo.mark_cancelled_items(request_id)
            await repo.set_request_status(
                request_id,
                status=BackfillRequestStatus.CANCELLED.value,
                error_code="backfill_cancelled",
                error_summary="执行期协作式取消",
                finished=True,
            )
            await session.commit()

    @staticmethod
    async def _reset_running_items(
        repo: DataBackfillRepository, request_id: str
    ) -> None:
        for item in await repo.list_items(request_id):
            if item.status == BackfillItemStatus.RUNNING.value:
                await repo.update_item(
                    item.id, status=BackfillItemStatus.PENDING.value
                )

    async def _release_exists(self, release_id: str) -> bool:
        async with self._session_maker() as session:
            header = await CatalogRepository(session).release_header(release_id)
            await session.commit()
        return header is not None

    async def _max_batch_id(self, dataset: str | None) -> int:
        """同步前的批次水位线(只校验本次新产生的批次,历史失败不误伤)."""

        if dataset is None:
            return 0
        async with self._session_maker() as session:
            stmt = select(func.max(ResearchSyncBatchModel.id)).where(
                ResearchSyncBatchModel.dataset == dataset
            )
            value = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
        return int(value or 0)

    async def _batch_ids_after(self, dataset: str | None, *, after_id: int) -> list[int]:
        if dataset is None:
            return []
        async with self._session_maker() as session:
            stmt = (
                select(ResearchSyncBatchModel.id)
                .where(
                    ResearchSyncBatchModel.dataset == dataset,
                    ResearchSyncBatchModel.id > after_id,
                )
                .order_by(ResearchSyncBatchModel.id.asc())
            )
            ids = [int(row[0]) for row in (await session.execute(stmt)).all()]
            await session.commit()
        return ids

    async def _assert_batch_quality(
        self, dataset: str | None, batch_ids: Sequence[int]
    ) -> None:
        """发布前校验最近批次的质量状态(质量门失败 → 不发布,请求失败)."""

        if dataset is None or not batch_ids:
            return
        async with self._session_maker() as session:
            stmt = select(
                ResearchSyncBatchModel.status,
                ResearchSyncBatchModel.quality_status,
                ResearchSyncBatchModel.error_summary,
            ).where(ResearchSyncBatchModel.id.in_(tuple(batch_ids)))
            rows = (await session.execute(stmt)).all()
            await session.commit()
        rejected = [
            row
            for row in rows
            if str(row[0]) in _QUALITY_FAILED_BATCH_STATUSES
            or (row[1] is not None and str(row[1]) in _QUALITY_FAILED_STATUSES)
        ]
        if rejected:
            raise ExecutorError(
                code="backfill_quality_failed",
                summary=(
                    f"{dataset} 最近批次质量门未通过: "
                    f"{rejected[0][2] or 'quality_status=' + str(rejected[0][1])}"
                )[:1000],
                retryable=False,
            )

    async def _resolve_market(self, item: DataBackfillRequestItemModel) -> str:
        """bars 批量任务的 market:请求标的的市场唯一时取之,否则按 A 股。"""

        if not item.symbols:
            return "a_share"
        async with self._session_maker() as session:
            stmt = (
                select(InstrumentModel.market)
                .where(InstrumentModel.code.in_(tuple(item.symbols)))
                .distinct()
            )
            markets = [str(row[0]) for row in (await session.execute(stmt)).all()]
            await session.commit()
        return markets[0] if len(markets) == 1 else "a_share"

    async def _publish_symbols(self, dataset: str) -> list[str]:
        """全市场请求的发布标的(按数据集域解析,与发布执行器 scope 门对齐)."""

        support = support_for(dataset)
        conditions: list[Any] = []
        if support.requires_a_share_stock:
            conditions.extend(
                [
                    InstrumentModel.market == "a_share",
                    InstrumentModel.instrument_type == "stock",
                ]
            )
        elif dataset == "convertible_metrics":
            conditions.extend(
                [
                    InstrumentModel.market == "a_share",
                    InstrumentModel.instrument_type == "convertible",
                ]
            )
        else:
            conditions.extend(
                [
                    InstrumentModel.instrument_type.in_(_BARS_INSTRUMENT_TYPES),
                    InstrumentModel.market.in_(("a_share", "future")),
                ]
            )
        async with self._session_maker() as session:
            stmt = (
                select(InstrumentModel.code)
                .where(*conditions)
                .order_by(InstrumentModel.code)
            )
            codes = [str(row[0]) for row in (await session.execute(stmt)).all()]
            await session.commit()
        return codes

    # ---------------------------------------------------------------- defaults
    def _default_executor(self, kind: str) -> JobExecutor:
        from finboard_backtest.background_jobs.dataset_sync import (
            DatasetSyncExecutor,
        )
        from finboard_backtest.background_jobs.executors._providers import (
            default_settings_factory,
        )
        from finboard_backtest.background_jobs.executors.bulk_download import (
            BulkDownloadExecutor,
        )
        from finboard_backtest.background_jobs.executors.dataset_publish import (
            DatasetPublishExecutor,
        )
        from finboard_backtest.background_jobs.executors.quality_repair import (
            QualityRepairExecutor,
        )

        settings_factory = self._settings_factory or default_settings_factory
        if kind == "dataset_sync":
            return DatasetSyncExecutor(
                session_maker=self._session_maker,
                settings_factory=settings_factory,
            )
        if kind == "bulk_download":
            return BulkDownloadExecutor(
                session_maker=self._session_maker,
                settings_factory=settings_factory,
            )
        if kind == "quality_repair":
            return QualityRepairExecutor(
                session_maker=self._session_maker,
                settings_factory=settings_factory,
            )
        if kind == "dataset_publish":
            return DatasetPublishExecutor(session_maker=self._session_maker)
        raise ExecutorError(
            code="backfill_unsupported_executor",
            summary=f"未知子执行器: {kind}",
            retryable=False,
        )


# ---------------------------------------------------------------------------
# 纯函数助手
# ---------------------------------------------------------------------------


def release_id_for(dataset: str, request_id: str) -> str:
    """发布身份:``DR-<dataset>-<request_id>``(同一请求重跑命中同一身份)."""

    return f"{_RELEASE_ID_PREFIX}{dataset}-{request_id}"


def version_for(request_id: str) -> str:
    """发布版本:``v-<request_id>``(与 release_id 一同确定性)."""

    return f"v-{request_id}"


def _require_request_id(job: JobRecord) -> str:
    raw = job.payload.get("request_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ExecutorError(
            code="invalid_payload",
            summary="data_backfill 任务 payload 必须包含 request_id(BF-...)",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw.strip()


def _child_progress(
    progress: ProgressCallback,
    *,
    item: DataBackfillRequestItemModel,
    index: int,
    total: int,
) -> ProgressCallback:
    """把子执行器的进度映射成父任务的 item 级进度(数值不越界)."""

    async def _callback(done: int, _total: int | None, phase: str | None) -> None:
        await progress(index - 1, total, f"data_backfill:{item.dataset}:{phase or ''}")

    return _callback


async def _noop_progress(done: int, total: int | None, phase: str | None) -> None:
    """发布阶段进度不进父进度条(item 粒度足够),避免数值回退。"""

    return None


__all__ = [
    "DataBackfillExecutor",
    "release_id_for",
    "version_for",
]
