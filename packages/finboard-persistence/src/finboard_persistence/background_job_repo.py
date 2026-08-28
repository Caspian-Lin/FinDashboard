"""统一后台任务队列仓储(issue #117 / #142)。

仿 :mod:`finboard_persistence.research_run_repo` 的状态机 + 幂等模式,
核心扩展是 ``claim_next`` —— 用 PostgreSQL ``FOR UPDATE SKIP LOCKED`` 让
多个 worker 进程安全地从同一队列领取任务而不重复执行。

边界:本仓储只读写 ``background_jobs`` 表(独立调度表,不建任何外键),
绝不触碰实盘 orders / fills / positions / audit_logs。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import BackgroundJobModel
from finboard_shared.background_jobs import (
    ARCHIVE_FILTER_VALUES,
    TERMINAL_STATUSES,
    BackgroundJobStatus,
)


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
        archived: str = "exclude",
    ) -> list[BackgroundJobModel]:
        """最近任务列表(issue #221:``archived`` 维度过滤)。

        ``archived`` 取值见 ``ARCHIVE_FILTER_VALUES``:``exclude``(默认)只看
        未归档;``only`` 只看已归档;``all`` 不区分。归档行数据不删除,
        单查 ``get`` 不受此参数影响。
        """

        if archived not in ARCHIVE_FILTER_VALUES:
            raise BackgroundJobPersistenceConflictError(
                f"未知归档过滤值: {archived}(合法 {sorted(ARCHIVE_FILTER_VALUES)})"
            )
        stmt = select(BackgroundJobModel)
        if archived == "only":
            stmt = stmt.where(BackgroundJobModel.archived_at.is_not(None))
        elif archived == "exclude":
            stmt = stmt.where(BackgroundJobModel.archived_at.is_(None))
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
        max_per_kind: Mapping[str, int] | None = None,
    ) -> list[BackgroundJobModel]:
        """用 ``FOR UPDATE SKIP LOCKED`` 领取 queued 任务,置 running + 写租约。

        多 worker 并发调用时,被其他事务锁住的行会被 ``SKIP LOCKED`` 跳过,
        因此同一任务不会被两个 worker 同时领取。``limit`` 允许一次领取多个
        以摊薄轮询成本(单 worker 主循环目前用 limit=1)。

        ``max_per_kind``(issue #144)按 ``kind`` 限制全局并发:先统计当前
        ``running`` 任务按 kind 分组的计数,已达到或超过上限的 kind 会被
        ``NOT IN`` 排除,本次不领取。空映射或 ``None`` 表示不限制。该限额
        在 SQL 层实现,多 worker 全局一致(不依赖内存信号量)。
        """

        stmt = (
            select(BackgroundJobModel)
            .where(BackgroundJobModel.status == BackgroundJobStatus.QUEUED.value)
            .order_by(
                BackgroundJobModel.priority.desc(),
                BackgroundJobModel.created_at.asc(),
                BackgroundJobModel.id.asc(),
            )
            .with_for_update(skip_locked=True)
        )
        if queues:
            stmt = stmt.where(BackgroundJobModel.queue.in_(tuple(queues)))
        over_limit_kinds = await self._over_limit_kinds(max_per_kind)
        if over_limit_kinds:
            stmt = stmt.where(BackgroundJobModel.kind.not_in(over_limit_kinds))
        stmt = stmt.limit(limit)
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

    async def _over_limit_kinds(
        self, max_per_kind: Mapping[str, int] | None
    ) -> tuple[str, ...]:
        """返回当前 running 计数已达 / 超过 ``max_per_kind`` 上限的 kind 集合。

        只统计 ``max_per_kind`` 显式声明的 kind(未声明的 kind 不限制)。
        用一条 ``GROUP BY kind`` 聚合查询,避免逐 kind 轮询。
        """

        if not max_per_kind:
            return ()
        capped = {k: cap for k, cap in max_per_kind.items() if cap > 0}
        if not capped:
            return ()
        count_stmt = (
            select(
                BackgroundJobModel.kind,
                func.count(BackgroundJobModel.id),
            )
            .where(BackgroundJobModel.status == BackgroundJobStatus.RUNNING.value)
            .where(BackgroundJobModel.kind.in_(tuple(capped)))
            .group_by(BackgroundJobModel.kind)
        )
        count_rows = (await self._session.execute(count_stmt)).all()
        counts: dict[str, int] = {row[0]: int(row[1]) for row in count_rows}
        return tuple(
            kind
            for kind, cap in capped.items()
            if counts.get(kind, 0) >= cap
        )

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
        """协作式取消请求。

        - running → cancel_requested(executor checkpoint 时退出);
        - queued / retry_waiting → cancelled(还没被 worker 领取,直接置终态);
        - 其余状态(终态 / interrupted)拒绝。
        """

        row = await self.get(job_id, for_update=True)
        if row is None:
            raise BackgroundJobPersistenceConflictError(f"后台任务不存在: {job_id}")
        if row.status == BackgroundJobStatus.RUNNING.value:
            return await self.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.RUNNING.value}),
                target=BackgroundJobStatus.CANCEL_REQUESTED.value,
            )
        if row.status in {
            BackgroundJobStatus.QUEUED.value,
            BackgroundJobStatus.RETRY_WAITING.value,
        }:
            return await self.transition(
                job_id,
                expected=frozenset(
                    {
                        BackgroundJobStatus.QUEUED.value,
                        BackgroundJobStatus.RETRY_WAITING.value,
                    }
                ),
                target=BackgroundJobStatus.CANCELLED.value,
            )
        raise BackgroundJobPersistenceConflictError(
            f"任务 {job_id} 当前状态 {row.status} 不支持取消"
        )

    # ------------------------------------------------------------------ archive
    async def archive(self, job_id: str) -> BackgroundJobModel:
        """归档终态任务(issue #221):``archived_at`` 置当前时间,从默认列表隐藏。

        - 仅 ``TERMINAL_STATUSES`` 可归档 —— 排队 / 运行中的任务被藏起来没人看见
          是事故温床,fail-closed 拒绝;
        - 幂等:已归档任务重复归档直接返回当前行;
        - 归档不删除任何数据,单查 ``get`` / ``archived=only|all`` 列表始终可达,
          ``unarchive`` 可恢复展示。
        """

        row = await self.get(job_id, for_update=True)
        if row is None:
            raise BackgroundJobPersistenceConflictError(f"后台任务不存在: {job_id}")
        if row.status not in TERMINAL_STATUSES:
            raise BackgroundJobPersistenceConflictError(
                f"任务 {job_id} 当前状态 {row.status} 不是终态,不支持归档"
            )
        if row.archived_at is None:
            now = datetime.now(UTC)
            row.archived_at = now
            row.updated_at = now
            await self._session.flush()
        return row

    async def unarchive(self, job_id: str) -> BackgroundJobModel:
        """取消归档(幂等):清空 ``archived_at``,任务重新出现在默认列表。"""

        row = await self.get(job_id, for_update=True)
        if row is None:
            raise BackgroundJobPersistenceConflictError(f"后台任务不存在: {job_id}")
        if row.archived_at is not None:
            row.archived_at = None
            row.updated_at = datetime.now(UTC)
            await self._session.flush()
        return row

    async def archive_bulk(
        self,
        *,
        kinds: Iterable[str] | None = None,
        statuses: Iterable[str] | None = None,
        queues: Iterable[str] | None = None,
        finished_before: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """批量归档未归档的终态任务,返回实际归档条数(issue #221)。

        - ``statuses`` 须为 ``TERMINAL_STATUSES`` 子集,空 / None 表示全部终态;
        - ``finished_before`` 只归档 ``finished_at`` 早于该时刻的行(终态行
          ``finished_at`` 由 ``transition`` 保证写入);
        - 按 ``created_at`` 从旧到新归档,单次 ``limit`` 夹紧 1..1000;
        - ``FOR UPDATE SKIP LOCKED`` 与 worker 维护路径并发安全。
        """

        final_statuses = set(statuses or TERMINAL_STATUSES)
        invalid = final_statuses - TERMINAL_STATUSES
        if invalid:
            raise BackgroundJobPersistenceConflictError(
                f"非终态状态不支持归档: {sorted(invalid)}"
            )
        stmt = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.status.in_(tuple(final_statuses)),
                BackgroundJobModel.archived_at.is_(None),
            )
            .order_by(BackgroundJobModel.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(max(1, min(1000, limit)))
        )
        if kinds is not None:
            stmt = stmt.where(BackgroundJobModel.kind.in_(tuple(kinds)))
        if queues is not None:
            stmt = stmt.where(BackgroundJobModel.queue.in_(tuple(queues)))
        if finished_before is not None:
            stmt = stmt.where(
                BackgroundJobModel.finished_at.is_not(None),
                BackgroundJobModel.finished_at <= finished_before,
            )
        rows = list((await self._session.execute(stmt)).scalars().all())
        now = datetime.now(UTC)
        for row in rows:
            row.archived_at = now
            row.updated_at = now
        await self._session.flush()
        return len(rows)

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

    async def requeue_due(
        self,
        now: datetime,
        *,
        backoff_seconds: float,
        limit: int = 50,
    ) -> tuple[list[BackgroundJobModel], list[BackgroundJobModel]]:
        """自动重排进入重试态已超过退避窗口的任务(issue #161)。

        - ``interrupted`` / ``retry_waiting`` 且 ``updated_at <= now - backoff``
          (即进入该状态已超过退避窗口)参与本轮;``updated_at`` 在进入这两个
          状态时由 ``transition`` 更新,天然就是状态进入时间;
        - 已归档(``archived_at`` 非空)的行不参与(issue #221 归档即冻结:
          不重排回队列、不强制失败,保持归档时的状态原样);
        - 还有剩余 attempt 的 → ``queued`` 重新入队(attempt 在下次领取时递增);
        - attempt 已耗尽 → ``failed``(终态,error_code=max_retries_exceeded)。

        ``FOR UPDATE SKIP LOCKED`` 保证多 worker 并发维护时同一行只被处理一次。
        返回 ``(requeued, exhausted)`` 两批行;调用方负责 commit。
        """

        cutoff = now - timedelta(seconds=max(backoff_seconds, 0.0))
        stmt = (
            select(BackgroundJobModel)
            .where(
                BackgroundJobModel.status.in_(
                    (
                        BackgroundJobStatus.INTERRUPTED.value,
                        BackgroundJobStatus.RETRY_WAITING.value,
                    )
                ),
                BackgroundJobModel.archived_at.is_(None),
                BackgroundJobModel.updated_at <= cutoff,
            )
            .order_by(BackgroundJobModel.updated_at.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        requeued: list[BackgroundJobModel] = []
        exhausted: list[BackgroundJobModel] = []
        for row in rows:
            row.updated_at = now
            if row.attempt >= row.max_attempts:
                row.status = BackgroundJobStatus.FAILED.value
                row.error_code = "max_retries_exceeded"
                row.error_summary = f"重试次数已达上限({row.max_attempts})"
                row.finished_at = now
                exhausted.append(row)
            else:
                row.status = BackgroundJobStatus.QUEUED.value
                requeued.append(row)
        await self._session.flush()
        return requeued, exhausted

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
