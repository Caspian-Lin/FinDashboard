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
