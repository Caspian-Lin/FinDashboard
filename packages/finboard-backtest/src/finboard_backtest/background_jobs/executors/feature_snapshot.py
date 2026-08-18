"""``feature_snapshot`` 执行器 —— 价格因子特征快照计算接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.research.FeatureSnapshotJobManager`` 内存态任务。worker
领取 ``kind=feature_snapshot`` 任务后,从 payload 重建 ``FrozenReleaseProvider`` 与
``build_price_feature_snapshot`` 参数,计算完成后用独立 session 发布到
``factor_feature_snapshots`` 表。

单并发约束(feature_snapshot=1)由 worker ``kind_concurrency`` 在 SQL 层保证,取代
旧 ``FeatureSnapshotJobManager`` 的进程内单 job gate。

边界:只写 ``factor_feature_snapshots`` 与 ``research_dataset_releases``(只读校验)
表,不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
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
from finboard_backtest.background_jobs.executors._runtime import (
    code_version,
)

if TYPE_CHECKING:
    pass


_DEFAULT_RELEASE_ROOT = "data_releases"


class FeatureSnapshotExecutor:
    """``kind=feature_snapshot`` 执行器。"""

    KIND = "feature_snapshot"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        release_root: str | Path | None = None,
        max_concurrency: int = 8,
        process_workers: int = 0,
    ) -> None:
        self._session_maker = session_maker
        self._release_root = Path(
            release_root
            if release_root is not None
            else os.getenv("FINBOARD_DATA_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT)
        )
        self._max_concurrency = max(1, min(64, int(max_concurrency)))
        self._process_workers = max(0, min(64, int(process_workers)))

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        dataset_release_id = _require_str(job, "dataset_release_id")
        additional_release_ids = job.payload.get("additional_release_ids")
        if additional_release_ids is None:
            additional_release_ids = []
        if not isinstance(additional_release_ids, list):
            raise ExecutorError(
                code="invalid_payload",
                summary="additional_release_ids 必须是字符串列表",
                retryable=False,
                context={"job_id": job.job_id},
            )
        decision_at_raw = _require_str(job, "decision_at")
        try:
            decision_at = datetime.fromisoformat(decision_at_raw).astimezone(UTC)
        except ValueError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary="decision_at 不是合法 ISO datetime",
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc

        momentum_lookback = _optional_int(job, "momentum_lookback", default=20)
        volatility_windows = job.payload.get("volatility_windows")
        if volatility_windows is not None and not isinstance(volatility_windows, list):
            raise ExecutorError(
                code="invalid_payload",
                summary="volatility_windows 必须是整数列表",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from finboard_backtest import build_price_feature_snapshot
        from finboard_data.releases import FrozenReleaseProvider, ReleaseDatasetKind
        from finboard_persistence import ResearchDatasetReleaseRepository
        from finboard_persistence.factor_lab_repo import FeatureSnapshotRepository

        # 1. 校验发布可用 + 决策时点在范围内
        async with self._session_maker() as session:
            release_repo = ResearchDatasetReleaseRepository(session)
            try:
                releases = [
                    await release_repo.require_usable(release_id)
                    for release_id in (dataset_release_id, *additional_release_ids)
                ]
            except Exception as exc:
                raise ExecutorError(
                    code="release_not_usable",
                    summary=f"数据发布 {dataset_release_id} 不可用: {exc}",
                    retryable=False,
                    context={"job_id": job.job_id, "release_id": dataset_release_id},
                ) from exc
            primary = releases[0]
            if not primary.start_date <= decision_at.date() <= primary.end_date:
                raise ExecutorError(
                    code="decision_at_out_of_range",
                    summary=(
                        f"decision_at 日期必须在 bars 主发布范围内: "
                        f"{primary.start_date}~{primary.end_date}"
                    ),
                    retryable=False,
                    context={"job_id": job.job_id},
                )

        total_holder: dict[str, int | None] = {"total": primary.symbol_count}
        on_progress = make_sync_progress(
            progress, phase_prefix="feature_snapshot", total_holder=total_holder
        )

        # 2. 构造冻结发布 provider + 计算快照
        try:
            providers = {
                release.release_id: FrozenReleaseProvider(
                    release_root=self._release_root,
                    release_id=release.release_id,
                    max_concurrency=self._max_concurrency,
                    expected_checksum=release.release_checksum,
                )
                for release in releases
            }
        except Exception as exc:
            raise ExecutorError(
                code="release_read_failed",
                summary=(
                    f"无法读取冻结发布文件 {dataset_release_id}: {exc}。"
                    f"请检查 FINBOARD_DATA_RELEASE_ROOT 目录与权限"
                ),
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc

        await progress(0, total_holder["total"], "feature_snapshot:start")
        try:
            if len(releases) == 1 or all(
                item.dataset_kind is ReleaseDatasetKind.BARS
                for item in releases[1:]
            ):
                snapshot = await build_price_feature_snapshot(
                    provider=providers[dataset_release_id],
                    decision_at=decision_at,
                    code_version=code_version(),
                    momentum_lookback=momentum_lookback,
                    volatility_windows=(
                        tuple(volatility_windows)
                        if isinstance(volatility_windows, list)
                        else (20, 60, 120)
                    ),
                    max_concurrency=self._max_concurrency,
                    process_workers=self._process_workers,
                    on_progress=on_progress,
                )
            else:
                from finboard_backtest.factor_lab import (
                    build_cross_section_feature_snapshot_from_releases,
                )

                snapshot = await build_cross_section_feature_snapshot_from_releases(
                    releases=releases,
                    providers=providers,
                    decision_at=decision_at,
                    code_version=code_version(),
                    max_concurrency=self._max_concurrency,
                    on_progress=on_progress,
                )
        except Exception as exc:
            raise ExecutorError(
                code="snapshot_build_failed",
                summary=f"特征快照计算失败: {exc}",
                retryable=False,
                context={"job_id": job.job_id, "release_id": dataset_release_id},
            ) from exc

        # 3. 发布到 DB(独立 session)
        async with self._session_maker() as job_session:
            try:
                await FeatureSnapshotRepository(job_session).publish(snapshot)
                await job_session.commit()
            except Exception as exc:
                await job_session.rollback()
                raise ExecutorError(
                    code="snapshot_publish_failed",
                    summary=f"特征快照发布失败: {exc}",
                    retryable=False,
                    context={"job_id": job.job_id},
                ) from exc

        await progress(
            total_holder.get("total") or 0,
            total_holder.get("total") or 0,
            "feature_snapshot:done",
        )
        return JobResult(
            status="succeeded",
            result_ref=snapshot.snapshot_id,
            progress_total=total_holder.get("total"),
        )


def _require_str(job: JobRecord, key: str) -> str:
    raw = job.payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise ExecutorError(
            code="invalid_payload",
            summary=f"feature_snapshot 任务 payload 必须包含合法 {key}",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


def _optional_int(job: JobRecord, key: str, *, default: int) -> int:
    raw = job.payload.get(key)
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ExecutorError(
            code="invalid_payload",
            summary=f"{key} 必须是整数",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


_: type[JobExecutor] = FeatureSnapshotExecutor

__all__ = ["FeatureSnapshotExecutor"]
