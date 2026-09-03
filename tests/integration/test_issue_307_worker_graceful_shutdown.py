"""worker 停机优雅信号路径集成测试(issue #307)。

真实 ``finboard worker run`` 子进程(与 ``finboard dev`` 托管 / ``--workers N``
supervisor 相同的进程组托管形态)+ 真实信号投递:

* grace=0(默认):停止信号 → 立即取消 in-flight → 退出码 0,任务留在
  running 由 lease 过期回收(现状语义,兜底链不变);
* grace>0:in-flight 完成后 worker 才退出(任务 succeeded,退出码 0);
* 宽限等待中第二次停止信号:立即强退(退出码 130),任务留在 running。

Windows:子进程经 ``CREATE_NEW_PROCESS_GROUP`` 托管(组根 = 子进程 pid),
优雅信号为 ``CTRL_BREAK_EVENT``(映射 SIGBREAK,由 ``run_worker`` 注册的
处理器接管;新进程组 Ctrl-C 默认禁用,与 dev/supervisor 生产形态一致)。
POSIX:子进程独立会话,优雅信号为 SIGTERM(``add_signal_handler`` 接管)。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator

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


async def _enqueue_echo(engine: AsyncEngine, sleep_seconds: float, steps: int) -> str:
    job_id = generate_background_job_id()
    payload: dict[str, object] = {"sleep_seconds": sleep_seconds, "steps": steps}
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
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
    return job_id


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


def _worker_env(test_db_url: str, grace_seconds: float) -> dict[str, str]:
    env = os.environ.copy()
    env["FINBOARD_DB_URL"] = test_db_url
    env["FINBOARD_TEST_DB_URL"] = test_db_url
    # 子 worker 关闭与研究运行时 / MCP 相关入口(worktree 隔离约定一致)。
    env["FINBOARD_OPENCODE_ENABLED"] = "false"
    env["FINBOARD_OPENCODE_WEB_ENABLED"] = "false"
    env["FINBOARD_MCP_ENABLED"] = "false"
    env["FINBOARD_WORKER_PROCESSES"] = "1"
    env["FINBOARD_WORKER_SHUTDOWN_GRACE_SECONDS"] = str(grace_seconds)
    env["PYTHONPATH"] = os.getcwd()
    return env


def _spawn_worker(test_db_url: str, grace_seconds: float) -> subprocess.Popen[bytes]:
    """按生产托管形态拉起单 worker 子进程(进程组根,优雅信号可达)。"""

    cmd = [
        sys.executable,
        "-m",
        "finboard_app.cli",
        "worker",
        "run",
        "--poll-interval",
        "0.2",
        "--max-concurrent",
        "1",
    ]
    env = _worker_env(test_db_url, grace_seconds)
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _send_graceful_stop(proc: subprocess.Popen[bytes]) -> None:
    """第一次停止信号:Windows CTRL_BREAK / POSIX SIGTERM(不硬杀)。"""

    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()


async def _wait_job_status(
    engine: AsyncEngine,
    job_id: str,
    statuses: set[str],
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    status = ""
    while time.monotonic() < deadline:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "select status from background_jobs where job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).first()
        status = str(row[0]) if row else ""
        if status in statuses:
            return status
        await asyncio.sleep(0.25)
    return status


async def _wait_exit_async(
    proc: subprocess.Popen[bytes], timeout_seconds: float
) -> int:
    return await asyncio.to_thread(_wait_exit_sync, proc, timeout_seconds)


def _wait_exit_sync(proc: subprocess.Popen[bytes], timeout_seconds: float) -> int:
    """在事件循环外等子进程退出,返回退出码(超时抛 TimeoutExpired)。"""

    return proc.wait(timeout=timeout_seconds)


def _ensure_terminated(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


async def _assert_job_left_running(engine: AsyncEngine, job_id: str) -> None:
    status = await _wait_job_status(
        engine,
        job_id,
        {BackgroundJobStatus.RUNNING.value},
        timeout_seconds=3.0,
    )
    assert status == BackgroundJobStatus.RUNNING.value, (
        f"未完成任务应留在 running 由 lease 回收,实际: {status!r}"
    )


class TestWorkerGracefulShutdown:
    @pytest.mark.timeout(180)
    async def test_grace_zero_cancels_inflight_and_exits_zero(
        self, engine: AsyncEngine, worker_db_url: str
    ) -> None:
        """默认(grace=0):停止信号 → 立即取消 in-flight,退出码 0(现状语义)。"""

        job_id = await _enqueue_echo(engine, sleep_seconds=60, steps=6)
        proc = _spawn_worker(worker_db_url, grace_seconds=0)
        try:
            status = await _wait_job_status(
                engine,
                job_id,
                {BackgroundJobStatus.RUNNING.value},
                timeout_seconds=90,
            )
            assert status == BackgroundJobStatus.RUNNING.value, "worker 未领取任务"
            with contextlib.suppress(OSError):
                _send_graceful_stop(proc)
            code = await _wait_exit_async(proc, timeout_seconds=60)
        finally:
            _ensure_terminated(proc)
        assert code == 0, f"优雅退出码应为 0,实际 {code}"
        await _assert_job_left_running(engine, job_id)

    @pytest.mark.timeout(180)
    async def test_grace_positive_waits_for_inflight_completion(
        self, engine: AsyncEngine, worker_db_url: str
    ) -> None:
        """grace>0:in-flight 完成后 worker 才退出,任务 succeeded,退出码 0。"""

        job_id = await _enqueue_echo(engine, sleep_seconds=8, steps=4)
        proc = _spawn_worker(worker_db_url, grace_seconds=30)
        try:
            status = await _wait_job_status(
                engine,
                job_id,
                {BackgroundJobStatus.RUNNING.value},
                timeout_seconds=90,
            )
            assert status == BackgroundJobStatus.RUNNING.value, "worker 未领取任务"
            with contextlib.suppress(OSError):
                _send_graceful_stop(proc)
            code = await _wait_exit_async(proc, timeout_seconds=60)
        finally:
            _ensure_terminated(proc)
        assert code == 0, f"优雅退出码应为 0,实际 {code}"
        final_status = await _wait_job_status(
            engine,
            job_id,
            {
                BackgroundJobStatus.SUCCEEDED.value,
                BackgroundJobStatus.RUNNING.value,
            },
            timeout_seconds=5.0,
        )
        assert final_status == BackgroundJobStatus.SUCCEEDED.value, (
            f"grace>0 应等 in-flight 完成而非取消,实际: {final_status!r}"
        )

    @pytest.mark.timeout(180)
    async def test_second_signal_forces_immediate_exit(
        self, engine: AsyncEngine, worker_db_url: str
    ) -> None:
        """宽限等待中第二次停止信号:立即强退(130),任务留在 running。"""

        job_id = await _enqueue_echo(engine, sleep_seconds=60, steps=6)
        proc = _spawn_worker(worker_db_url, grace_seconds=20)
        try:
            status = await _wait_job_status(
                engine,
                job_id,
                {BackgroundJobStatus.RUNNING.value},
                timeout_seconds=90,
            )
            assert status == BackgroundJobStatus.RUNNING.value, "worker 未领取任务"
            with contextlib.suppress(OSError):
                _send_graceful_stop(proc)
            await asyncio.sleep(1.5)  # 让第一次信号进入 drain 宽限等待
            start = time.monotonic()
            with contextlib.suppress(OSError):
                if sys.platform == "win32":
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
            code = await _wait_exit_async(proc, timeout_seconds=15)
            elapsed = time.monotonic() - start
        finally:
            _ensure_terminated(proc)
        assert code == 130, f"强退退出码应为 130,实际 {code}"
        assert elapsed < 10, f"第二次信号应立即强退(宽限 20s),实际 {elapsed:.1f}s"
        await _assert_job_left_running(engine, job_id)
