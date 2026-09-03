"""worker 停机优雅信号路径 + 可选 drain 宽限单测(issue #307)。

覆盖:
* settings / WorkerConfig 默认宽限 0(默认即时停机,行为零回归);
* ``BackgroundWorker._drain`` 三分支:grace=0 立即取消 / grace>0 等 in-flight
  完成至宽限上限 / 超时与第二次停止信号的取消兜底(返回 True = 强退);
* ``route_stop_signal`` 第一/第二次信号路由与 ``run_worker`` 退出码翻译
  (强退 SystemExit(130),优雅 0 不抛);
* Windows 信号处理器注册后恢复(不残留全局状态);
* ``finboard dev`` / supervisor 侧:``_stop_dev_process`` 先优雅后强杀、
  ``--shutdown-grace`` 转发、supervisor 收敛宽限对齐 settings。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from finboard_app.cli import (
    _WORKER_STOP_FALLBACK_GRACE_SECONDS,
    _dev_worker_stop_grace_seconds,
    _stop_dev_process,
    _supervise_worker_processes,
    _worker_child_command,
)
from finboard_app.config import Settings
from finboard_backtest.background_jobs.worker import (
    WORKER_FORCE_EXIT_CODE,
    BackgroundWorker,
    WorkerConfig,
    route_stop_signal,
    run_worker,
)


def _long_sleep_command(seconds: int) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _make_worker(grace: float) -> BackgroundWorker:
    """构造只用于 _drain 语义验证的 worker(engine 等不参与 drain 路径)。"""

    config = WorkerConfig(
        worker_id="t-307",
        poll_interval_seconds=0.05,
        max_concurrent=1,
        lease_timeout_seconds=60,
        heartbeat_interval_seconds=10,
        shutdown_grace_seconds=grace,
    )
    return BackgroundWorker(
        engine=MagicMock(),
        session_maker=MagicMock(),
        registry=MagicMock(),
        config=config,
    )


async def _job(
    finished: list[bool],
    cancelled_flags: list[bool],
    started: asyncio.Event,
    delay: float,
) -> None:
    started.set()
    try:
        await asyncio.sleep(delay)
        finished.append(True)
    except asyncio.CancelledError:
        cancelled_flags.append(True)
        raise


async def _drain_with_inflight(
    grace: float, delay: float
) -> tuple[BackgroundWorker, asyncio.Task[None], list[bool], list[bool]]:
    worker = _make_worker(grace)
    finished: list[bool] = []
    cancelled: list[bool] = []
    started = asyncio.Event()
    task = asyncio.create_task(_job(finished, cancelled, started, delay))
    # 等任务真正进入 await(而非 cancel 落在首个调度前),断言才有意义。
    await asyncio.wait_for(started.wait(), timeout=5.0)
    worker._inflight.add(task)
    return worker, task, finished, cancelled


class TestDefaults:
    def test_settings_default_grace_is_zero(self) -> None:
        """默认 0:即时停机,行为零回归(用户拍板 2026-09-03)。"""

        assert Settings().worker_shutdown_grace_seconds == 0.0

    def test_settings_rejects_negative_grace(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal"):
            Settings(worker_shutdown_grace_seconds=-1.0)

    def test_worker_config_default_grace_is_zero(self) -> None:
        config = WorkerConfig(
            worker_id="t",
            poll_interval_seconds=0.1,
            max_concurrent=1,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10,
        )
        assert config.shutdown_grace_seconds == 0.0


class TestDrainSemantics:
    async def test_grace_zero_cancels_immediately(self) -> None:
        """grace=0(默认):立即取消 in-flight,不等待(现状语义)。"""

        worker, task, finished, cancelled = await _drain_with_inflight(0.0, 5.0)
        start = time.monotonic()
        force = await worker._drain()
        elapsed = time.monotonic() - start
        assert force is False
        assert elapsed < 2.0, f"grace=0 应立即取消,实际等待 {elapsed:.2f}s"
        assert cancelled == [True]
        assert finished == []
        assert task.cancelled()

    async def test_no_inflight_returns_immediately(self) -> None:
        worker = _make_worker(5.0)
        assert await worker._drain() is False

    async def test_grace_positive_waits_for_completion(self) -> None:
        """grace>0:in-flight 自然完成,worker 才退出(不被取消)。"""

        worker, task, finished, cancelled = await _drain_with_inflight(10.0, 0.5)
        start = time.monotonic()
        force = await worker._drain()
        elapsed = time.monotonic() - start
        assert force is False
        assert elapsed >= 0.4, "in-flight 未完成前不得退出"
        assert finished == [True]
        assert cancelled == []
        assert not task.cancelled()

    async def test_grace_timeout_cancels_remaining(self) -> None:
        """宽限超时:取消剩余任务兜底(与 grace=0 同一终点语义),退出码 0。"""

        worker, task, finished, cancelled = await _drain_with_inflight(0.5, 30.0)
        start = time.monotonic()
        force = await worker._drain()
        elapsed = time.monotonic() - start
        assert force is False
        assert 0.4 <= elapsed < 5.0, f"应在宽限上限附近取消,实际 {elapsed:.2f}s"
        assert cancelled == [True]
        assert finished == []
        assert task.cancelled()

    async def test_force_stop_during_grace_returns_true(self) -> None:
        """第二次停止信号:跳过剩余宽限立即取消,drain 报告强退。"""

        worker, task, finished, cancelled = await _drain_with_inflight(30.0, 30.0)
        drain_task = asyncio.create_task(worker._drain())
        await asyncio.sleep(0.5)
        start = time.monotonic()
        worker.request_force_stop()
        force = await drain_task
        elapsed = time.monotonic() - start
        assert force is True
        assert elapsed < 2.0, f"第二次信号应立即强退,实际等待 {elapsed:.2f}s"
        assert cancelled == [True]
        assert finished == []
        assert task.cancelled()

    async def test_force_before_drain_short_circuits_grace(self) -> None:
        """进入 drain 前已收到第二次信号(双击 Ctrl-C):直接强退不等待。"""

        worker, task, _finished, cancelled = await _drain_with_inflight(30.0, 30.0)
        worker.request_force_stop()
        start = time.monotonic()
        force = await worker._drain()
        assert force is True
        assert time.monotonic() - start < 2.0
        assert cancelled == [True]
        assert task.cancelled()


class TestSignalRoutingAndExitCode:
    def test_route_first_signal_requests_stop(self) -> None:
        worker = _make_worker(5.0)
        route_stop_signal(worker)
        assert worker.stop_requested is True
        assert worker.force_stop_requested is False

    def test_route_second_signal_requests_force(self) -> None:
        worker = _make_worker(5.0)
        route_stop_signal(worker)
        route_stop_signal(worker)
        assert worker.force_stop_requested is True

    def _run_worker_kwargs(self) -> dict[str, object]:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return {
            "engine": engine,
            "session_maker": MagicMock(),
            "registry": MagicMock(),
            "config": WorkerConfig(
                worker_id="t",
                poll_interval_seconds=0.1,
                max_concurrent=1,
                lease_timeout_seconds=60,
                heartbeat_interval_seconds=10,
                shutdown_grace_seconds=5.0,
            ),
        }

    async def test_run_worker_force_returns_exit_code_130(self) -> None:
        """第二次信号强退 → SystemExit(130);engine 仍被优雅 dispose。"""

        kwargs = self._run_worker_kwargs()
        with patch.object(BackgroundWorker, "run", AsyncMock(return_value=True)):
            with pytest.raises(SystemExit) as exc_info:
                await run_worker(**kwargs)  # type: ignore[arg-type]
        assert exc_info.value.code == WORKER_FORCE_EXIT_CODE == 130
        kwargs["engine"].dispose.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_run_worker_graceful_returns_without_raise(self) -> None:
        """优雅完成(含宽限超时兜底)退出码 0:不抛异常正常返回。"""

        kwargs = self._run_worker_kwargs()
        with patch.object(BackgroundWorker, "run", AsyncMock(return_value=False)):
            await run_worker(**kwargs)  # type: ignore[arg-type]
        kwargs["engine"].dispose.assert_awaited_once()  # type: ignore[attr-defined]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows signal.signal 路径")
    async def test_windows_handlers_restored_after_run(self) -> None:
        """Windows:注册的 SIGINT/SIGBREAK 处理器在 run 结束后恢复,不留全局状态。"""

        previous = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGBREAK)
        }
        kwargs = self._run_worker_kwargs()
        with patch.object(BackgroundWorker, "run", AsyncMock(return_value=False)):
            await run_worker(**kwargs)  # type: ignore[arg-type]
        for sig, handler in previous.items():
            assert signal.getsignal(sig) is handler


class TestDevStopProcess:
    def _popen_process_group(self, seconds: int) -> subprocess.Popen[bytes]:
        """与 dev/supervisor 相同的进程组托管形态(CTRL_BREAK 可达)。"""

        if sys.platform == "win32":
            return subprocess.Popen(
                _long_sleep_command(seconds),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return subprocess.Popen(_long_sleep_command(seconds), start_new_session=True)

    def test_dev_worker_stop_grace_uses_settings(self) -> None:
        settings = Settings(worker_shutdown_grace_seconds=30.0)
        assert _dev_worker_stop_grace_seconds(settings) == 30.0

    def test_dev_worker_stop_grace_fallback_when_zero(self) -> None:
        """settings=0 时用 10s 兜底窗口(覆盖「立即取消 + 退出」耗时)。"""

        assert (
            _dev_worker_stop_grace_seconds(Settings())
            == _WORKER_STOP_FALLBACK_GRACE_SECONDS
            == 10.0
        )

    @pytest.mark.timeout(30)
    def test_graceful_signal_exits_child_without_taskkill(self) -> None:
        """graceful>0:CTRL_BREAK/SIGTERM 先行,子进程自行退出,无需强杀。"""

        process = self._popen_process_group(60)
        try:
            _stop_dev_process(process, graceful_seconds=10.0)
        finally:
            if process.poll() is None:
                process.kill()
        assert process.poll() is not None

    def test_windows_graceful_timeout_falls_back_to_taskkill(self) -> None:
        """Windows:优雅信号等待超时后按整树 taskkill 兜底。"""

        process = MagicMock(spec=subprocess.Popen)
        process.pid = 12345
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        with (
            patch("finboard_app.cli.sys.platform", "win32"),
            patch("finboard_app.cli.os.kill") as kill,
            patch("finboard_app.cli.subprocess.run") as run,
        ):
            _stop_dev_process(process, graceful_seconds=1.0)
        kill.assert_called_once_with(12345, signal.CTRL_BREAK_EVENT)
        run.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T", "/F"],
            check=False,
            capture_output=True,
        )

    def test_windows_graceful_completion_skips_taskkill(self) -> None:
        """Windows:优雅信号后子进程自行退出,不再 taskkill。"""

        process = MagicMock(spec=subprocess.Popen)
        process.pid = 12345
        process.poll.return_value = None
        with (
            patch("finboard_app.cli.sys.platform", "win32"),
            patch("finboard_app.cli.os.kill") as kill,
            patch("finboard_app.cli.subprocess.run") as run,
        ):
            _stop_dev_process(process, graceful_seconds=5.0)
        kill.assert_called_once_with(12345, signal.CTRL_BREAK_EVENT)
        run.assert_not_called()

    def test_windows_zero_grace_keeps_instant_taskkill(self) -> None:
        """graceful=0:保持既有立即 taskkill(默认配置的强杀兜底路径)。"""

        process = MagicMock(spec=subprocess.Popen)
        process.pid = 12345
        process.poll.return_value = None
        with (
            patch("finboard_app.cli.sys.platform", "win32"),
            patch("finboard_app.cli.subprocess.run") as run,
        ):
            _stop_dev_process(process, graceful_seconds=0.0)
        run.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T", "/F"],
            check=False,
            capture_output=True,
        )


class TestSupervisorGrace:
    def test_child_command_forwards_shutdown_grace(self) -> None:
        cmd = _worker_child_command({"worker_shutdown_grace_seconds": "30"})
        assert cmd[5:] == ["--shutdown-grace", "30"]

    @pytest.mark.unit
    @pytest.mark.timeout(30)
    def test_supervisor_graceful_signal_converges_children(self) -> None:
        """supervisor grace>0:先发优雅信号,子进程自行收敛 → 退出码 0。"""

        calls = {"n": 0}

        def interrupting_sleep(_: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt

        real_kill = os.kill
        with (
            patch("finboard_app.cli.time.sleep", side_effect=interrupting_sleep),
            patch("finboard_app.cli.os.kill", side_effect=real_kill) as kill,
        ):
            code = _supervise_worker_processes(
                _long_sleep_command(30), 1, shutdown_grace_seconds=5.0
            )
        assert code == 0
        if sys.platform == "win32":
            # Windows 上优雅信号 = 进程组 CTRL_BREAK,而非 terminate 硬杀。
            assert kill.call_count >= 1
            assert kill.call_args.args[1] is signal.CTRL_BREAK_EVENT

    @pytest.mark.unit
    def test_worker_run_passes_settings_grace_to_supervisor(self) -> None:
        """``--workers N`` supervisor 收敛宽限与 settings 对齐(不再固定 10s)。"""

        from finboard_app.cli import worker_run

        ctx = MagicMock()
        ctx.obj = Settings(worker_processes=2, worker_shutdown_grace_seconds=42.0)
        with (
            patch("finboard_app.cli._supervise_worker_processes", return_value=0) as sup,
            pytest.raises(typer.Exit),
        ):
            worker_run(ctx)
        _, count = sup.call_args.args
        assert count == 2
        assert sup.call_args.kwargs["shutdown_grace_seconds"] == 42.0
