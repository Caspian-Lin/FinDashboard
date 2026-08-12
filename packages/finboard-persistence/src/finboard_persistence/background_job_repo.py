"""统一后台任务队列仓储(issue #117 / #142)。

仿 :mod:`finboard_persistence.research_run_repo` 的状态机 + 幂等模式,
核心扩展是 ``claim_next`` —— 用 PostgreSQL ``FOR UPDATE SKIP LOCKED`` 让
多个 worker 进程安全地从同一队列领取任务而不重复执行。

边界:本仓储只读写 ``background_jobs`` 表(独立调度表,不建任何外键),
绝不触碰实盘 orders / fills / positions / audit_logs。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import BackgroundJobModel
from finboard_shared.background_jobs import BackgroundJobStatus


class BackgroundJobPersistenceConflictError(RuntimeError):
    """数据库中的幂等内容或状态与请求冲突。"""


class BackgroundJobRepository:
    """``background_jobs`` 表的读写入口;``__init__`` 只持有 session,不自己 commit。

    遵循现有约定(见 ``research_run_repo.py``):调用方负责 commit / rollback,
    本仓储只在事务内 ``flush`` 让行可见并占据行锁。``checkpoint()`` 提供给
    worker 在每轮领取 / 心跳后显式提交。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def checkpoint(self) -> None:
        await self._session.commit()

    # ------------------------------------------------------------------ create
    async def create_or_get(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        kind: str,
        queue: str,
        status: str,
        priority: int,
        payload: dict[str, object],
        payload_checksum: str,
        max_attempts: int,
        requested_by: str,
    ) -> tuple[BackgroundJobModel, bool]:
        """幂等创建。同 idempotency_key 已存在则返回旧记录(created=False)。"""

        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:
            existing = await self.get(job_id)
        if existing is not None:
            if existing.payload_checksum != payload_checksum:
                raise BackgroundJobPersistenceConflictError(
                    "相同 job_id/idempotency_key 对应不同 payload"
                )
            return existing, False
        row = BackgroundJobModel(
            job_id=job_id,
            idempotency_key=idempotency_key,
            kind=kind,
            queue=queue,
            status=status,
            priority=priority,
            payload=payload,
            payload_checksum=payload_checksum,
            max_attempts=max_attempts,
            requested_by=requested_by,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
            return row, True
        except IntegrityError:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                existing = await self.get(job_id)
            if existing is None:
                raise
            if existing.payload_checksum != payload_checksum:
                raise BackgroundJobPersistenceConflictError(
                    "并发创建命中相同身份但 payload 不同"
                ) from None
            return existing, False

    # -------------------------------------------------------------------- read
    async def get(
        self, job_id: str, *, for_update: bool = False
    ) -> BackgroundJobModel | None:
        stmt = select(BackgroundJobModel).where(BackgroundJobModel.job_id == job_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> BackgroundJobModel | None:
        stmt = select(BackgroundJobModel).where(
            BackgroundJobModel.idempotency_key == idempotency_key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(
        self,
        *,
        kinds: Iterable[str] | None = None,
        statuses: Iterable[str] | None = None,
        queues: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[BackgroundJobModel]:
        stmt = select(BackgroundJobModel)
        if kinds is not None:
            stmt = stmt.where(BackgroundJobModel.kind.in_(tuple(kinds)))
        if statuses is not None:
            stmt = stmt.where(BackgroundJobModel.status.in_(tuple(statuses)))
        if queues is not None:
            stmt = stmt.where(BackgroundJobModel.queue.in_(tuple(queues)))
        stmt = stmt.order_by(
            BackgroundJobModel.created_at.desc(), BackgroundJobModel.id.desc()
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    # --------------------------------------------------------------- transition
    async def transition(
        self,
        job_id: str,
        *,
        expected: frozenset[str],
        target: str,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> BackgroundJobModel:
        """状态机转移。``expected`` 守卫当前状态;副作用设置 started_at / finished_at。

        ``expected`` 传空 frozenset 表示不校验当前状态(仅在 reclaim / recover 等
        强制回收场景使用,正常业务路径不要这么写)。
        """

        row = await self.get(job_id, for_update=True)
        if row is None:
            raise BackgroundJobPersistenceConflictError(f"后台任务不存在: {job_id}")
        if expected and row.status not in expected:
            raise BackgroundJobPersistenceConflictError(
                f"任务 {job_id} 当前状态 {row.status} 不在 {sorted(expected)}"
            )
        now = datetime.now(UTC)
        row.status = target
        row.error_code = error_code
        row.error_summary = error_summary
        row.updated_at = now
        if target == BackgroundJobStatus.RUNNING.value and row.started_at is None:
            row.started_at = now
        if target in {
            BackgroundJobStatus.SUCCEEDED.value,
            BackgroundJobStatus.FAILED.value,
            BackgroundJobStatus.CANCELLED.value,
            BackgroundJobStatus.INTERRUPTED.value,
        }:
            row.finished_at = now
        await self._session.flush()
        return row

    # --------------------------------------------------------------- claim_next
    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_until: datetime,
        queues: Sequence[str] | None = None,
        limit: int = 1,
    ) -> list[BackgroundJobModel]:
        """用 ``FOR UPDATE SKIP LOCKED`` 领取 queued 任务,置 running + 写租约。

        多 worker 并发调用时,被其他事务锁住的行会被 ``SKIP LOCKED`` 跳过,
        因此同一任务不会被两个 worker 同时领取。``limit`` 允许一次领取多个
        以摊薄轮询成本(单 worker 主循环目前用 limit=1)。
        """

        stmt = (
            select(BackgroundJobModel)
            .where(BackgroundJobModel.status == BackgroundJobStatus.QUEUED.value)
            .order_by(
                BackgroundJobModel.priority.desc(),
                BackgroundJobModel.created_at.asc(),
                BackgroundJobModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if queues:
            stmt = stmt.where(BackgroundJobModel.queue.in_(tuple(queues)))
        rows = list((await self._session.execute(stmt)).scalars().all())
        now = datetime.now(UTC)
        for row in rows:
            row.status = BackgroundJobStatus.RUNNING.value
            row.worker_id = worker_id
            row.lease_until = lease_until
            row.heartbeat_at = now
            row.attempt += 1
            row.updated_at = now
            if row.started_at is None:
                row.started_at = now
        await self._session.flush()
        return rows

    # ---------------------------------------------------------------- progress
    async def update_progress(
        self,
        job_id: str,
        *,
        done: int,
        total: int | None = None,
        phase: str | None = None,
    ) -> BackgroundJobModel:
        """更新进度,强制 ``done`` 单调递增、不超 ``total``(复刻 FeatureSnapshotJob)。"""

        row = await self.get(job_id, for_update=True)
        if row is None:
            raise BackgroundJobPersistenceConflictError(f"后台任务不存在: {job_id}")
        if total is not None and total >= 0:
            row.progress_total = max(row.progress_total, total)
        ceiling = row.progress_total if row.progress_total else done
        row.progress_done = min(max(row.progress_done, done), ceiling) if ceiling else max(
            row.progress_done, done
        )
        if phase is not None:
            row.phase = phase
        row.heartbeat_at = datetime.now(UTC)
        row.updated_at = row.heartbeat_at
        await self._session.flush()
        return row

    async def update_heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_until: datetime,
    ) -> BackgroundJobModel | None:
        """worker 在执行期间周期性续约;返回 None 表示任务已不在 running。"""

        row = await self.get(job_id, for_update=True)
        if row is None:
            return None
        now = datetime.now(UTC)
        row.heartbeat_at = now
        row.lease_until = lease_until
        row.worker_id = worker_id
        row.updated_at = now
        await self._session.flush()
        return row

    # ---------------------------------------------------------- reclaim / cancel
    async def reclaim_stale(
        self, now: datetime, *, target: str = BackgroundJobStatus.INTERRUPTED.value
    ) -> list[BackgroundJobModel]:
        """把 lease 已过期的 running / cancel_requested 任务回收为 interrupted。

        worker 崩溃或断电后,其持有的 lease 会自然过期;下次启动 / 由其他 worker
        调本方法即可把它们标记为 interrupted,供重新入队或人工介入。``target``
        可由调用方覆盖(例如直接重置为 queued 做自动重排,但默认保守地停在 interrupted)。
        """

        stmt = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.status.in_(
                    (
                        BackgroundJobStatus.RUNNING.value,
                        BackgroundJobStatus.CANCEL_REQUESTED.value,
                    )
                ),
                BackgroundJobModel.lease_until.is_not(None),
                BackgroundJobModel.lease_until < now,
            )
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            row.status = target
            row.finished_at = now
            row.updated_at = now
        await self._session.flush()
        return rows

    async def request_cancel(self, job_id: str) -> BackgroundJobModel:
        """协作式取消请求:running → cancel_requested,由 executor checkpoint 时退出。"""

        return await self.transition(
            job_id,
            expected=frozenset({BackgroundJobStatus.RUNNING.value}),
            target=BackgroundJobStatus.CANCEL_REQUESTED.value,
        )

    async def finish(
        self,
        job_id: str,
        *,
        target: str,
        result_ref: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> BackgroundJobModel:
        """worker 在 executor 返回后统一收口(running → succeeded/failed/cancelled)。"""

        row = await self.transition(
            job_id,
            expected=frozenset(
                {
                    BackgroundJobStatus.RUNNING.value,
                    BackgroundJobStatus.CANCEL_REQUESTED.value,
                }
            ),
            target=target,
            error_code=error_code,
            error_summary=error_summary,
        )
        if result_ref is not None:
            row.result_ref = result_ref
        await self._session.flush()
        return row

    async def requeue_interrupted(self, job_id: str) -> BackgroundJobModel:
        """把 interrupted 任务重置为 queued(供 recover 命令或重试策略使用)。"""

        return await self.transition(
            job_id,
            expected=frozenset(
                {
                    BackgroundJobStatus.INTERRUPTED.value,
                    BackgroundJobStatus.RETRY_WAITING.value,
                }
            ),
            target=BackgroundJobStatus.QUEUED.value,
        )

    @staticmethod
    def model_payload(row: BackgroundJobModel) -> dict[str, Any]:
        return {
            "job_id": row.job_id,
            "kind": row.kind,
            "queue": row.queue,
            "status": row.status,
            "priority": row.priority,
            "payload": row.payload,
            "progress_total": row.progress_total,
            "progress_done": row.progress_done,
            "phase": row.phase,
            "result_ref": row.result_ref,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
            "attempt": row.attempt,
            "max_attempts": row.max_attempts,
            "worker_id": row.worker_id,
            "requested_by": row.requested_by,
        }


__all__ = [
    "BackgroundJobPersistenceConflictError",
    "BackgroundJobRepository",
]
