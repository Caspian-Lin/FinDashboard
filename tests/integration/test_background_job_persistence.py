"""统一后台任务队列 PostgreSQL 全链路集成测试(issue #117 / #142)。

覆盖:
* Repository 状态机(transition expected 守卫 / started_at / finished_at 副作用);
* ``claim_next`` 用 ``FOR UPDATE SKIP LOCKED``,两 worker 并发不重复领取;
* ``reclaim_stale`` 把过期 lease 的 running 回收为 interrupted;
* ``request_cancel`` 协作式取消(running → cancel_requested);
* ``update_progress`` 单调递增且不超 total;
* worker 进程端到端:echo kind queued → running → succeeded,result_ref 落库。

所有用例共用一个 module 级 engine,通过独立 session 操作 background_jobs 表,
用例结束清表,与其他集成 module 隔离。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import EchoExecutor
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
    Base,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)


def _checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue(
    engine: AsyncEngine,
    *,
    job_id: str | None = None,
    kind: str = "echo",
    queue: str = "default",
    priority: int = 0,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> str:
    payload = payload or {}
    job_id = job_id or generate_background_job_id()
    idem = idempotency_key or f"idem-{job_id}"
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=idem,
            kind=kind,
            queue=queue,
            status=BackgroundJobStatus.QUEUED.value,
            priority=priority,
            payload=payload,
            payload_checksum=_checksum(payload),
            max_attempts=3,
            requested_by="tester",
        )
        await repo.checkpoint()
    return job_id


def _fresh_lease() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=600)


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
async def engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    """module 级 engine:建表 → 测试 → 清表。

    本 module 不复用 conftest 的 db_session,因为 claim_next 并发 / reclaim /
    worker e2e 都需要跨多个独立 session 操作同一表。
    """

    eng = create_async_engine(_db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    """每个用例开始前清空 background_jobs,保证用例间隔离。"""

    async with engine.begin() as conn:
        await conn.execute(text("delete from background_jobs"))


# ---------------------------------------------------------------- Repository


class TestIdempotencyDeadRecreate:
    """issue #371:幂等键命中 failed/cancelled 死行时放行同键新建。

    succeeded 仍返回旧记录(幂等命中);interrupted 保持单行语义
    (lease 回收重排 / #305 replay 依赖);唯一性由部分唯一索引承载
    (只约束活跃行)。
    """

    async def _seed(self, engine: AsyncEngine, key: str, status: str) -> str:
        """建一个 queued 任务并直接置为指定状态(绕过状态机守卫)。"""
        job_id = generate_background_job_id()
        payload: dict[str, object] = {"x": 1}
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.create_or_get(
                job_id=job_id,
                idempotency_key=key,
                kind="echo",
                queue="default",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=_checksum(payload),
                max_attempts=3,
                requested_by="tester",
            )
            await repo.checkpoint()
        async with engine.begin() as conn:
            await conn.execute(
                text("update background_jobs set status = :s where job_id = :j"),
                {"s": status, "j": job_id},
            )
        return job_id

    async def _create_again(
        self, engine: AsyncEngine, key: str, payload: dict[str, object]
    ) -> tuple[Any, bool]:
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row, created = await repo.create_or_get(
                job_id=generate_background_job_id(),
                idempotency_key=key,
                kind="echo",
                queue="default",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=_checksum(payload),
                max_attempts=3,
                requested_by="tester",
            )
            await repo.checkpoint()
        return row, created

    async def test_failed_row_allows_new_job(self, engine: AsyncEngine) -> None:
        """失败尸体不再挡同参数重试:新建 job,旧行保留。"""
        key = "idem-371-failed"
        old_id = await self._seed(engine, key, "failed")
        row, created = await self._create_again(engine, key, {"x": 1})
        assert created is True
        assert row.job_id != old_id
        assert row.status == BackgroundJobStatus.QUEUED.value
        # 同键多行时,查询语义 = 活跃行优先(返回新建行)
        async with session_factory(engine)() as session:
            looked = await BackgroundJobRepository(
                session
            ).get_by_idempotency_key(key)
        assert looked is not None
        assert looked.job_id == row.job_id

    async def test_cancelled_row_allows_new_job(self, engine: AsyncEngine) -> None:
        key = "idem-371-cancelled"
        old_id = await self._seed(engine, key, "cancelled")
        row, created = await self._create_again(engine, key, {"x": 1})
        assert created is True
        assert row.job_id != old_id

    async def test_succeeded_row_still_idempotent(self, engine: AsyncEngine) -> None:
        """succeeded 命中返回旧记录(对内容寻址构建即缓存命中)。"""
        key = "idem-371-succeeded"
        old_id = await self._seed(engine, key, "succeeded")
        row, created = await self._create_again(engine, key, {"x": 1})
        assert created is False
        assert row.job_id == old_id

    async def test_interrupted_row_still_idempotent(self, engine: AsyncEngine) -> None:
        """interrupted 保持单行语义(lease 回收重排 / replay 通道依赖)。"""
        key = "idem-371-interrupted"
        old_id = await self._seed(engine, key, "interrupted")
        row, created = await self._create_again(engine, key, {"x": 1})
        assert created is False
        assert row.job_id == old_id

    async def test_dead_row_different_payload_conflicts(
        self, engine: AsyncEngine
    ) -> None:
        key = "idem-371-conflict"
        await self._seed(engine, key, "failed")
        with pytest.raises(BackgroundJobPersistenceConflictError):
            await self._create_again(engine, key, {"x": 2})

    async def test_partial_index_replaces_column_unique(
        self, engine: AsyncEngine
    ) -> None:
        """schema 断言:部分唯一索引在位,旧列级 UNIQUE 已移除。

        本地旧测试库若残留建表时的 UNIQUE(表不重建则 create_all 不改),
        本用例会失败——按惯例 drop background_jobs 表重跑即可。
        """
        async with engine.begin() as conn:
            indexes = (
                await conn.execute(
                    text(
                        "select indexname from pg_indexes "
                        "where tablename = 'background_jobs'"
                    )
                )
            ).fetchall()
            legacy = (
                await conn.execute(
                    text(
                        "select conname from pg_constraint "
                        "where conrelid = 'background_jobs'::regclass "
                        "and contype = 'u' "
                        "and conname = 'background_jobs_idempotency_key_key'"
                    )
                )
            ).fetchall()
        names = {r[0] for r in indexes}
        assert "uq_background_jobs_idempotency_active" in names
        assert legacy == []


class TestRepositoryStateMachine:
    async def test_create_or_get_is_idempotent(self, engine: AsyncEngine) -> None:
        repo_make = BackgroundJobRepository  # 便于阅读
        job_id = generate_background_job_id()
        payload: dict[str, object] = {"x": 1}
        async with session_factory(engine)() as session:
            repo = repo_make(session)
            row1, created1 = await repo.create_or_get(
                job_id=job_id,
                idempotency_key=f"idem-{job_id}",
                kind="echo",
                queue="default",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=_checksum(payload),
                max_attempts=3,
                requested_by="tester",
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = repo_make(session)
            row2, created2 = await repo.create_or_get(
                job_id=generate_background_job_id(),
                idempotency_key=f"idem-{job_id}",
                kind="echo",
                queue="default",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=payload,
                payload_checksum=_checksum(payload),
                max_attempts=3,
                requested_by="tester",
            )
            await repo.checkpoint()
        assert created1 is True
        assert created2 is False
        assert row1.job_id == row2.job_id

    async def test_create_or_get_rejects_checksum_mismatch(
        self, engine: AsyncEngine
    ) -> None:
        job_id = generate_background_job_id()
        initial_payload: dict[str, object] = {"x": 1}
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.create_or_get(
                job_id=job_id,
                idempotency_key=f"idem-{job_id}",
                kind="echo",
                queue="default",
                status=BackgroundJobStatus.QUEUED.value,
                priority=0,
                payload=initial_payload,
                payload_checksum=_checksum(initial_payload),
                max_attempts=3,
                requested_by="tester",
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            mismatch_payload: dict[str, object] = {"x": 2}
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.create_or_get(
                    job_id=generate_background_job_id(),
                    idempotency_key=f"idem-{job_id}",
                    kind="echo",
                    queue="default",
                    status=BackgroundJobStatus.QUEUED.value,
                    priority=0,
                    payload=mismatch_payload,
                    payload_checksum=_checksum(mismatch_payload),
                    max_attempts=3,
                    requested_by="tester",
                )

    async def test_transition_expected_guard_and_side_effects(
        self, engine: AsyncEngine
    ) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            # 当前状态 queued,用 running 的 expected 集合守卫应失败
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.transition(
                    job_id,
                    expected=frozenset({BackgroundJobStatus.RUNNING.value}),
                    target=BackgroundJobStatus.SUCCEEDED.value,
                )
            # 正确转移 queued -> running -> succeeded
            await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.RUNNING.value,
            )
            row = await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.RUNNING.value}),
                target=BackgroundJobStatus.SUCCEEDED.value,
            )
            await repo.checkpoint()
        assert row.status == BackgroundJobStatus.SUCCEEDED.value
        assert row.started_at is not None
        assert row.finished_at is not None

    async def test_transition_missing_job_raises(self, engine: AsyncEngine) -> None:
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.transition(
                    "BJ-DOESNOTEXIST",
                    expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                    target=BackgroundJobStatus.RUNNING.value,
                )


# ---------------------------------------------------------------- claim_next


class TestClaimNextSkipLocked:
    async def test_claim_marks_running_and_sets_lease(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(worker_id="w1", lease_until=_fresh_lease())
            await repo.checkpoint()
        assert len(rows) == 1
        assert rows[0].job_id == job_id
        assert rows[0].status == BackgroundJobStatus.RUNNING.value
        assert rows[0].worker_id == "w1"
        assert rows[0].lease_until is not None
        assert rows[0].attempt == 1

    async def test_priority_ordering(self, engine: AsyncEngine) -> None:
        low = await _enqueue(engine, priority=0)
        high = await _enqueue(engine, priority=10)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w1", lease_until=_fresh_lease(), limit=10
            )
            await repo.checkpoint()
        assert [r.job_id for r in rows] == [high, low]

    async def test_two_workers_never_claim_same_job(self, engine: AsyncEngine) -> None:
        job_ids = [await _enqueue(engine) for _ in range(5)]
        lease = _fresh_lease()
        claimed: list[str] = []

        async def claim(worker: str) -> list[str]:
            async with session_factory(engine)() as session:
                repo = BackgroundJobRepository(session)
                rows = await repo.claim_next(
                    worker_id=worker, lease_until=lease, limit=1
                )
                await repo.checkpoint()
                return [r.job_id for r in rows]

        for _ in range(10):
            claimed.extend(await claim("w1"))
            claimed.extend(await claim("w2"))
            if len(claimed) >= 5:
                break
        assert sorted(claimed) == sorted(job_ids)
        assert len(claimed) == len(set(claimed))  # 无重复领取

    async def test_claim_filters_by_queue(self, engine: AsyncEngine) -> None:
        await _enqueue(engine, queue="cpu")
        io_job = await _enqueue(engine, queue="io")
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id="w-io",
                lease_until=_fresh_lease(),
                queues=("io",),
                limit=10,
            )
            await repo.checkpoint()
        assert [r.job_id for r in rows] == [io_job]


# ---------------------------------------------------------------- reclaim


class TestReclaimStale:
    async def test_reclaim_expired_lease(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            # lease 设到过去,模拟 worker 崩溃
            await repo.claim_next(
                worker_id="w-dead",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            reclaimed = await repo.reclaim_stale(datetime.now(UTC))
            await repo.checkpoint()
        assert len(reclaimed) == 1
        assert reclaimed[0].job_id == job_id
        assert reclaimed[0].status == BackgroundJobStatus.INTERRUPTED.value

    async def test_reclaim_ignores_fresh_lease(self, engine: AsyncEngine) -> None:
        await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(
                worker_id="w-alive", lease_until=_fresh_lease()
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            reclaimed = await repo.reclaim_stale(datetime.now(UTC))
            await repo.checkpoint()
        assert reclaimed == []


# ---------------------------------------------------------------- cancel / progress


class TestRequestCancel:
    async def test_running_to_cancel_requested(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(worker_id="w1", lease_until=_fresh_lease())
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.request_cancel(job_id)
            await repo.checkpoint()
        assert row.status == BackgroundJobStatus.CANCEL_REQUESTED.value

    async def test_cancel_non_running_raises(self, engine: AsyncEngine) -> None:
        await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.request_cancel("BJ-NONEXISTENT-CANCEL")

    async def test_cancel_queued_marks_cancelled(self, engine: AsyncEngine) -> None:
        """还没被 worker 领取的任务直接置终态 cancelled(issue #161)。"""
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.request_cancel(job_id)
            await repo.checkpoint()
        assert row.status == BackgroundJobStatus.CANCELLED.value
        assert row.finished_at is not None

    async def test_cancel_retry_waiting_marks_cancelled(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.RETRY_WAITING.value,
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.request_cancel(job_id)
            await repo.checkpoint()
        assert row.status == BackgroundJobStatus.CANCELLED.value

    async def test_cancel_terminal_raises(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.SUCCEEDED.value,
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.request_cancel(job_id)


async def _age_status_entry(engine: AsyncEngine, job_id: str, seconds: int = 60) -> None:
    """把任务进入当前状态的时间(updated_at)改到过去,模拟已等待 seconds 秒。"""
    async with session_factory(engine)() as session:
        await session.execute(
            text(
                "update background_jobs set updated_at = now() - make_interval(secs => :s) "
                "where job_id = :jid"
            ),
            {"s": seconds, "jid": job_id},
        )
        await session.commit()


# ---------------------------------------------------------------- requeue_due


class TestRequeueDue:
    """retry_waiting / interrupted 自动重排(issue #161)。"""

    async def test_requeue_interrupted_after_backoff(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            # lease 设到过去,模拟 worker 崩溃后留下的僵尸 running
            await repo.claim_next(
                worker_id="w-dead",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            await repo.checkpoint()
            await repo.reclaim_stale(datetime.now(UTC))
            await repo.checkpoint()
        await _age_status_entry(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            requeued, exhausted = await repo.requeue_due(
                datetime.now(UTC), backoff_seconds=30
            )
            await repo.checkpoint()
        assert [r.job_id for r in requeued] == [job_id]
        assert exhausted == []
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.QUEUED.value

    async def test_requeue_retry_waiting_after_backoff(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.RETRY_WAITING.value,
            )
            await repo.checkpoint()
        await _age_status_entry(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            requeued, _ = await repo.requeue_due(
                datetime.now(UTC), backoff_seconds=30
            )
            await repo.checkpoint()
        assert [r.job_id for r in requeued] == [job_id]

    async def test_fresh_retry_waiting_stays_put(self, engine: AsyncEngine) -> None:
        """进入重试态还没超过退避窗口的任务不参与本轮。"""
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.transition(
                job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.RETRY_WAITING.value,
            )
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            requeued, exhausted = await repo.requeue_due(
                datetime.now(UTC), backoff_seconds=30
            )
            await repo.checkpoint()
        assert requeued == []
        assert exhausted == []
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.RETRY_WAITING.value

    async def test_exhausted_attempts_marks_failed(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            await session.execute(
                text(
                    "update background_jobs set attempt = max_attempts, "
                    "status = :s, updated_at = now() - interval '60 seconds' "
                    "where job_id = :jid"
                ),
                {
                    "s": BackgroundJobStatus.RETRY_WAITING.value,
                    "jid": job_id,
                },
            )
            await BackgroundJobRepository(session).checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            requeued, exhausted = await repo.requeue_due(
                datetime.now(UTC), backoff_seconds=30
            )
            await repo.checkpoint()
        assert requeued == []
        assert [r.job_id for r in exhausted] == [job_id]
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.FAILED.value
        assert row.error_code == "max_retries_exceeded"


async def _make_terminal(
    engine: AsyncEngine,
    job_id: str,
    *,
    target: str = BackgroundJobStatus.SUCCEEDED.value,
    finished_at: datetime | None = None,
) -> None:
    """把 queued 任务置为终态(interrupted 须经 reclaim 路径写 finished_at)。"""

    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.transition(
            job_id,
            expected=frozenset({BackgroundJobStatus.QUEUED.value}),
            target=target,
        )
        if finished_at is not None:
            await session.execute(
                text("update background_jobs set finished_at = :f where job_id = :jid"),
                {"f": finished_at, "jid": job_id},
            )
        await repo.checkpoint()


class TestArchive:
    """归档:隐藏不删除 + 仅终态 + 冻结(issue #221)。"""

    async def test_archive_terminal_sets_archived_at(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        await _make_terminal(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.archive(job_id)
            await repo.checkpoint()
        assert row.archived_at is not None
        # 数据未删除:单查仍可达
        async with session_factory(engine)() as session:
            fetched = await BackgroundJobRepository(session).get(job_id)
        assert fetched is not None
        assert fetched.archived_at is not None

    async def test_archive_is_idempotent(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        await _make_terminal(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            first = await repo.archive(job_id)
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            second = await repo.archive(job_id)
            await repo.checkpoint()
        assert second.archived_at == first.archived_at

    async def test_archive_non_terminal_rejected(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)  # queued
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            with pytest.raises(BackgroundJobPersistenceConflictError, match="终态"):
                await repo.archive(job_id)

    async def test_archive_missing_job_raises(self, engine: AsyncEngine) -> None:
        async with session_factory(engine)() as session:
            with pytest.raises(BackgroundJobPersistenceConflictError, match="不存在"):
                await BackgroundJobRepository(session).archive("BJ-MISSING")

    async def test_unarchive_clears_and_is_idempotent(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        await _make_terminal(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.archive(job_id)
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.unarchive(job_id)
            await repo.checkpoint()
        assert row.archived_at is None
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            again = await repo.unarchive(job_id)  # 未归档再 unarchive 幂等
            await repo.checkpoint()
        assert again.archived_at is None

    async def test_list_recent_archived_filters(self, engine: AsyncEngine) -> None:
        visible = await _enqueue(engine)
        hidden = await _enqueue(engine)
        await _make_terminal(engine, visible)
        await _make_terminal(engine, hidden)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.archive(hidden)
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            default_rows = await repo.list_recent(limit=10)
            only_rows = await repo.list_recent(limit=10, archived="only")
            all_rows = await repo.list_recent(limit=10, archived="all")
        assert [r.job_id for r in default_rows] == [visible]
        assert [r.job_id for r in only_rows] == [hidden]
        assert {r.job_id for r in all_rows} == {visible, hidden}

    async def test_archive_bulk_counts_and_filters(
        self, engine: AsyncEngine
    ) -> None:
        old = await _enqueue(engine)
        recent = await _enqueue(engine)
        queued = await _enqueue(engine)
        await _make_terminal(engine, old, finished_at=datetime.now(UTC) - timedelta(days=7))
        await _make_terminal(engine, recent, finished_at=datetime.now(UTC))
        # queued 保持非终态:批量归档只匹配终态,不应被归档
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            count = await repo.archive_bulk(
                finished_before=datetime.now(UTC) - timedelta(days=1),
            )
            await repo.checkpoint()
        assert count == 1
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            remaining = await repo.list_recent(limit=10)
        assert {r.job_id for r in remaining} == {recent, queued}
        # 批量归档非终态子集被拒
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            with pytest.raises(BackgroundJobPersistenceConflictError):
                await repo.archive_bulk(statuses=["running"])

    async def test_archive_bulk_respects_limit_and_skips_archived(
        self, engine: AsyncEngine
    ) -> None:
        ids = [await _enqueue(engine) for _ in range(3)]
        for jid in ids:
            await _make_terminal(engine, jid)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            first = await repo.archive_bulk(limit=2)
            await repo.checkpoint()
            second = await repo.archive_bulk(limit=2)  # 已归档的不再计数
            await repo.checkpoint()
        assert first == 2
        assert second == 1

    async def test_requeue_due_skips_archived(self, engine: AsyncEngine) -> None:
        """归档即冻结:interrupted 归档后不被自动重排回队列。"""
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(
                worker_id="w-dead",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            await repo.checkpoint()
            await repo.reclaim_stale(datetime.now(UTC))  # → interrupted(终态)
            await repo.checkpoint()
            await repo.archive(job_id)
            await repo.checkpoint()
        await _age_status_entry(engine, job_id)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            requeued, exhausted = await repo.requeue_due(
                datetime.now(UTC), backoff_seconds=30
            )
            await repo.checkpoint()
        assert requeued == []
        assert exhausted == []
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.INTERRUPTED.value
        assert row.archived_at is not None


class TestUpdateProgress:
    async def test_monotonic_and_clamped(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(worker_id="w1", lease_until=_fresh_lease())
            await repo.checkpoint()
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.update_progress(job_id, done=2, total=5, phase="p1")
            await repo.update_progress(job_id, done=4, total=5, phase="p2")
            # 倒退应被忽略(取 max);phase=None 不覆盖(保留最近一次非空值 p2)
            row = await repo.update_progress(job_id, done=1, total=5)
            await repo.checkpoint()
        assert row.progress_done == 4
        assert row.progress_total == 5
        assert row.phase == "p2"
        # 超过 total 应被钳制到 total
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.update_progress(job_id, done=999, total=5)
            await repo.checkpoint()
        assert row.progress_done == 5


# ---------------------------------------------------------------- worker e2e


class TestBackgroundWorkerEndToEnd:
    async def test_echo_job_succeeds_end_to_end(self, engine: AsyncEngine) -> None:
        echo_payload: dict[str, object] = {"steps": 2}
        job_id = await _enqueue(engine, payload=echo_payload)
        registry = JobExecutorRegistry()
        registry.register("echo", EchoExecutor())
        worker = BackgroundWorker(
            engine=engine,
            session_maker=session_factory(engine),
            registry=registry,
            config=WorkerConfig(
                worker_id="w-test",
                poll_interval_seconds=0.05,
                max_concurrent=2,
                lease_timeout_seconds=60,
                heartbeat_interval_seconds=10.0,
            ),
        )
        await _drain_worker(worker)
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.SUCCEEDED.value
        assert row.result_ref is not None
        assert row.result_ref.startswith("echo:")
        assert row.progress_done == 2

    async def test_worker_reclaims_stale_on_start(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(
                worker_id="w-dead",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            await repo.checkpoint()
        worker = BackgroundWorker(
            engine=engine,
            session_maker=session_factory(engine),
            registry=JobExecutorRegistry(),
            config=WorkerConfig(
                worker_id="w-recover",
                poll_interval_seconds=0.05,
                max_concurrent=1,
                lease_timeout_seconds=60,
                heartbeat_interval_seconds=10.0,
            ),
        )
        await worker._recover_stale()
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.INTERRUPTED.value

    async def test_unknown_kind_marks_failed(self, engine: AsyncEngine) -> None:
        job_id = await _enqueue(engine, kind="mystery")
        registry = JobExecutorRegistry()  # 不注册 mystery
        worker = BackgroundWorker(
            engine=engine,
            session_maker=session_factory(engine),
            registry=registry,
            config=WorkerConfig(
                worker_id="w-test",
                poll_interval_seconds=0.05,
                max_concurrent=1,
                lease_timeout_seconds=60,
                heartbeat_interval_seconds=10.0,
            ),
        )
        await _drain_worker(worker)
        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.FAILED.value
        assert row.error_code == "unknown_kind"

    async def test_periodic_maintenance_unblocks_saturated_kind(
        self, engine: AsyncEngine
    ) -> None:
        """僵尸 running 不再永久堵死 per-kind 并发(issue #161)。

        流程:job A 以过期 lease 的 running 残留(模拟崩溃 worker)占用 echo
        单并发 → job B queued 被堵住;worker 周期维护回收 A 为 interrupted →
        退避后自动重排 → A/B 都被消费为 succeeded。
        """

        a_id = await _enqueue(engine)
        b_id = await _enqueue(engine)
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.claim_next(
                worker_id="w-zombie",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            await repo.checkpoint()
        registry = JobExecutorRegistry()
        registry.register("echo", EchoExecutor())
        worker = BackgroundWorker(
            engine=engine,
            session_maker=session_factory(engine),
            registry=registry,
            config=WorkerConfig(
                worker_id="w-maint",
                poll_interval_seconds=0.05,
                max_concurrent=2,
                lease_timeout_seconds=60,
                heartbeat_interval_seconds=10.0,
                kind_concurrency={"echo": 1},
                maintenance_interval_seconds=0.05,
                retry_backoff_seconds=0.01,
            ),
        )
        run_task = asyncio.create_task(worker.run())
        try:
            for _ in range(2000):
                async with session_factory(engine)() as session:
                    rows = await BackgroundJobRepository(session).list_recent(
                        limit=10
                    )
                if len(rows) >= 2 and all(
                    r.status == BackgroundJobStatus.SUCCEEDED.value for r in rows
                ):
                    break
                await asyncio.sleep(0.02)
        finally:
            worker.request_stop()
            await run_task
        async with session_factory(engine)() as session:
            final_rows = {
                r.job_id: r.status
                for r in await BackgroundJobRepository(session).list_recent(
                    limit=10
                )
            }
        assert final_rows.get(a_id) == BackgroundJobStatus.SUCCEEDED.value
        assert final_rows.get(b_id) == BackgroundJobStatus.SUCCEEDED.value


async def _drain_worker(worker: BackgroundWorker) -> None:
    """驱动一轮领取并等待所有 in-flight task 结束(含心跳取消清理)。

    本 module 用 ``asyncio_default_test_loop_scope=module``,测试间共享事件循环。
    这里显式 fill + join,确保每个 worker 测试返回时没有遗留的后台 task 污染下一个用例。
    """

    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


# ---------------------------------------------------------------- 分页(issue #373)


class TestListRecentPagination:
    """list_recent offset + count_recent(issue #373 任务中心服务端分页)。"""

    async def test_offset_window_and_count_consistency(self, engine: AsyncEngine) -> None:
        for i in range(5):
            await _enqueue(engine, kind="echo", job_id=f"BJ-PAGE{i:012d}")

        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            page1 = await repo.list_recent(limit=2, offset=0)
            page2 = await repo.list_recent(limit=2, offset=2)
            page3 = await repo.list_recent(limit=2, offset=4)
            total = await repo.count_recent()
            filtered_total = await repo.count_recent(kinds=["echo"])
            other_total = await repo.count_recent(kinds=["backtest_run"])

        ids = [r.job_id for r in page1] + [r.job_id for r in page2] + [
            r.job_id for r in page3
        ]
        assert len(ids) == 5  # 三窗拼出全量
        assert len(set(ids)) == 5  # 窗口不重叠
        assert total == 5
        assert filtered_total == 5
        assert other_total == 0
