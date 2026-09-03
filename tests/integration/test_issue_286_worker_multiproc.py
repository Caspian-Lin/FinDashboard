"""多 worker 进程基础设施集成测试(issue #286)。

覆盖:
* ``claim_next`` 并发竞态 —— 两个并发 claimer 同 kind(max_per_kind=1)只有
  一个能拿到任务(单并发约束跨进程全局生效的 SQL 层锁定);
* 双 worker 进程真实竞争领取互不重复(subprocess 拉起 CLI 单 worker 形态,
  Windows 实测);
* ``--workers 2`` supervisor 端到端:父进程拉起两个子 worker 消费队列后
  正常收敛。

kind_concurrency 的单并发语义对 research_code_run / validation_experiment 等
kind 是正确性约束(沙箱资源 / 一次性揭盲),跨进程必须全局生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_persistence import (
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
    kind: str,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> str:
    job_id = generate_background_job_id()
    payload = payload or {}
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=idempotency_key or f"idem-{job_id}",
            kind=kind,
            queue="default",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=_checksum(payload),
            max_attempts=3,
            requested_by="tester",
        )
        await repo.checkpoint()
    return job_id


def _fresh_lease(seconds: int = 600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture(scope="module")
async def engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(_db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("delete from background_jobs"))


@pytest.fixture
def worker_db_url(_db_url: str) -> str:
    """给子 worker 进程用的测试库 URL(下划线前缀参数会触发 PT019)。"""

    return _db_url


async def _claim_batch(
    engine: AsyncEngine,
    *,
    worker_id: str,
    max_per_kind: dict[str, int] | None,
    limit: int = 10,
) -> list[str]:
    """独立 session 上领取一批任务并提交(等价一个 worker 进程的一次领取)。"""

    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        rows = await repo.claim_next(
            worker_id=worker_id,
            lease_until=_fresh_lease(),
            limit=limit,
            max_per_kind=max_per_kind,
        )
        await repo.checkpoint()
        return [row.job_id for row in rows]


class TestConcurrentClaimPerKind:
    async def test_concurrent_claimers_single_per_kind(self, engine: AsyncEngine) -> None:
        """两个并发 claimer 同 kind(max_per_kind=1):总共只能领取一个。

        竞态窗口:两个 claimer 的「running 计数」查询都先于任一提交执行时,
        各自看到 0 个 running —— 不加跨事务串行化时双方各领一个,单并发
        约束被击穿(research_code_run 双容器 / validation_experiment 重复
        揭盲的正确性红线)。多轮重复放大窗口命中概率。
        """

        for _ in range(5):
            # 每轮清表重入:上轮被 cap 拦在队列的行不参与下一轮计数。
            async with engine.begin() as conn:
                await conn.execute(text("delete from background_jobs"))
            await _enqueue(engine, kind="validation_experiment")
            await _enqueue(engine, kind="validation_experiment")
            claimed = await asyncio.gather(
                _claim_batch(
                    engine, worker_id="w-a", max_per_kind={"validation_experiment": 1}
                ),
                _claim_batch(
                    engine, worker_id="w-b", max_per_kind={"validation_experiment": 1}
                ),
            )
            total = sum(len(batch) for batch in claimed)
            assert total == 1, (
                f"并发 claimer 同 kind 单并发约束被击穿:共领取 {total} 个 "
                f"(w-a={len(claimed[0])}, w-b={len(claimed[1])})"
            )

    async def test_concurrent_claimers_after_finish_next_claimable(
        self, engine: AsyncEngine
    ) -> None:
        """上一批 claimed 全部终态后,下一个 claimer 能继续领取(不卡死队列)。"""

        await _enqueue(engine, kind="validation_experiment")
        await _enqueue(engine, kind="validation_experiment")
        first = await _claim_batch(
            engine, worker_id="w-a", max_per_kind={"validation_experiment": 1}
        )
        assert len(first) == 1
        # 只收口被领取的那一行(它此刻是 running);cap 拦下的第二行保持 queued。
        async with engine.begin() as conn:
            await conn.execute(
                text("update background_jobs set status='succeeded' where status='running'")
            )
        second = await _claim_batch(
            engine, worker_id="w-b", max_per_kind={"validation_experiment": 1}
        )
        assert len(second) == 1

    async def test_concurrent_claimers_unlimited_kind_not_blocked(
        self, engine: AsyncEngine
    ) -> None:
        """未声明 max_per_kind 的 kind 不受影响(并发领取互不重复)。"""

        for index in range(4):
            await _enqueue(engine, kind="echo", payload={"n": index})
        claimed = await asyncio.gather(
            _claim_batch(engine, worker_id="w-a", max_per_kind=None),
            _claim_batch(engine, worker_id="w-b", max_per_kind=None),
        )
        total = sum(len(batch) for batch in claimed)
        assert total == 4
        assert len(set(claimed[0]) & set(claimed[1])) == 0, "同一任务被两个 claimer 领取"


# ------------------------------------------------------- 真实子进程形态


def _worker_env(test_db_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["FINBOARD_DB_URL"] = test_db_url
    env["FINBOARD_TEST_DB_URL"] = test_db_url
    # 子 worker 关闭与研究运行时 / MCP 相关入口(worktree 隔离约定一致)
    env["FINBOARD_OPENCODE_ENABLED"] = "false"
    env["FINBOARD_OPENCODE_WEB_ENABLED"] = "false"
    env["FINBOARD_MCP_ENABLED"] = "false"
    env["FINBOARD_WORKER_PROCESSES"] = "1"
    env["PYTHONPATH"] = os.getcwd()
    return env


class _WorkerSubprocess:
    """拉起 / 收敛 ``finboard worker run`` 子进程的测试夹具。"""

    def __init__(
        self, test_db_url: str, *, count: int, extra_args: Sequence[str]
    ) -> None:
        self._cmd = [
            sys.executable,
            "-m",
            "finboard_app.cli",
            "worker",
            "run",
            "--poll-interval",
            "0.2",
            *extra_args,
        ]
        self._env = _worker_env(test_db_url)
        self._count = count
        self.procs: list[subprocess.Popen[bytes]] = []

    def __enter__(self) -> _WorkerSubprocess:
        self.procs = [
            subprocess.Popen(
                self._cmd,
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(self._count)
        ]
        return self

    def __exit__(self, *exc_info: object) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
        for proc in self.procs:
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)


def _spawn_workers(test_db_url: str, count: int) -> _WorkerSubprocess:
    """拉起 count 个真实单 worker 子进程(Windows 实测形态)。"""

    return _WorkerSubprocess(
        test_db_url, count=count, extra_args=["--max-concurrent", "1"]
    )


async def _wait_terminal(
    engine: AsyncEngine, expected: int, timeout_seconds: float
) -> list[tuple[str, str, int, str | None]]:
    """轮询直到 expected 个任务全部到达终态,返回 (job_id, status, attempt, worker_id)。"""

    deadline = time.monotonic() + timeout_seconds
    rows: list[tuple[str, str, int, str | None]] = []
    while time.monotonic() < deadline:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "select job_id, status, attempt, worker_id from background_jobs"
                )
            )
            rows = [
                (str(r[0]), str(r[1]), int(r[2]), str(r[3]) if r[3] is not None else None)
                for r in result
            ]
        if len(rows) == expected and all(
            status
            in (
                BackgroundJobStatus.SUCCEEDED.value,
                BackgroundJobStatus.FAILED.value,
                BackgroundJobStatus.CANCELLED.value,
                BackgroundJobStatus.INTERRUPTED.value,
            )
            for _, status, _, _ in rows
        ):
            return rows
        await asyncio.sleep(0.2)
    return rows


class TestDualWorkerProcessCompetition:
    @pytest.mark.timeout(180)
    async def test_two_real_worker_processes_claim_without_duplicates(
        self, engine: AsyncEngine, worker_db_url: str
    ) -> None:
        """两个真实 worker 子进程竞争领取:全部 succeeded 且互不重复。

        Windows(spawn,无 fork)实测:子进程经 ``python -m finboard_app.cli
        worker run`` 重新拉起 CLI 单 worker 形态(与 ``finboard dev`` 托管
        worker 同一入口),各自独立 engine / session_maker / worker_id。
        """

        total = 40
        for index in range(total):
            await _enqueue(engine, kind="echo", payload={"n": index})

        with _spawn_workers(worker_db_url, 2) as workers:
            procs = workers.procs
            assert len(procs) == 2
            rows = await _wait_terminal(engine, expected=total, timeout_seconds=150)

        assert len(rows) == total, f"只看到 {len(rows)}/{total} 个任务"
        statuses = [status for _, status, _, _ in rows]
        assert all(s == BackgroundJobStatus.SUCCEEDED.value for s in statuses), (
            f"存在非 succeeded 终态: {set(statuses)}"
        )
        attempts = [attempt for _, _, attempt, _ in rows]
        assert all(a == 1 for a in attempts), (
            f"attempt > 1 意味着发生过回收重排(重复执行风险): {attempts}"
        )
        worker_ids = {wid for _, _, _, wid in rows if wid is not None}
        assert len(worker_ids) == 2, (
            f"期望两个子 worker 都实际领取到任务,实际: {worker_ids}"
        )
        assert all(p.returncode in (0, 1) for p in procs), (
            f"worker 子进程异常退出: {[p.returncode for p in procs]}"
        )
        # 说明:POSIX 上 SIGTERM 走 run_worker 的优雅停止 → 0;Windows 上
        # 测试夹具 terminate()(TerminateProcess)产生退出码 1 —— 任务已全部
        # succeeded 且 attempt==1,退出码差异只反映夹具的强制收敛方式。

    @pytest.mark.timeout(240)
    async def test_supervisor_workers_flag_end_to_end(
        self, engine: AsyncEngine, worker_db_url: str
    ) -> None:
        """``worker run --workers 2`` 端到端:父进程拉起进程树消费队列。

        与上一测试的差别:子进程由被测的 supervisor 分支(而非测试)拉起,
        验证真实部署形态;收敛用 ``taskkill /T /F``(Windows)杀整棵树,
        防止子 worker 成为孤儿进程继续轮询。
        """

        total = 12
        for index in range(total):
            await _enqueue(engine, kind="echo", payload={"n": index})

        cmd = [
            sys.executable,
            "-m",
            "finboard_app.cli",
            "worker",
            "run",
            "--workers",
            "2",
            "--poll-interval",
            "0.2",
            "--max-concurrent",
            "1",
        ]

        proc = subprocess.Popen(  # noqa: ASYNC220
            cmd,
            env=_worker_env(worker_db_url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            rows = await _wait_terminal(engine, expected=total, timeout_seconds=200)
        finally:
            if sys.platform == "win32":
                subprocess.run(  # noqa: ASYNC221
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=15)

        assert len(rows) == total, f"只看到 {len(rows)}/{total} 个任务"
        statuses = [status for _, status, _, _ in rows]
        assert all(s == BackgroundJobStatus.SUCCEEDED.value for s in statuses), (
            f"存在非 succeeded 终态: {set(statuses)}"
        )
        worker_ids = {wid for _, _, _, wid in rows if wid is not None}
        assert len(worker_ids) == 2, (
            f"supervisor 的两个子 worker 都应领取到任务,实际: {worker_ids}"
        )
