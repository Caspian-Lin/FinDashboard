"""``finboard worker run --workers N`` 多进程形态单测(issue #286)。

覆盖:
* 子进程命令构造:CLI 覆盖项原样转发、--workers 不转发(子进程固定单 worker,
  防递归进程树);
* supervisor:全部子进程干净退出 → 0;任一子进程非零退出 → 1;
  Ctrl-C(等待循环被打断)→ terminate/kill 收敛子进程 → 0;
* CLI 分流:``--workers >1`` 走 supervisor 并以 typer.Exit 透传退出码;
  默认(worker_processes=1)保持单进程 asyncio.run 路径零回归。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
import typer

from finboard_app.config import Settings


def _long_sleep_command(seconds: int) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _exit_code_command(code: int) -> list[str]:
    return [sys.executable, "-c", f"raise SystemExit({code})"]


class TestWorkerChildCommand:
    def test_forwards_explicit_cli_overrides(self) -> None:
        from finboard_app.cli import _worker_child_command

        cmd = _worker_child_command(
            {
                "worker_poll_interval_seconds": "0.5",
                "worker_max_concurrent": "2",
                "worker_queues": "research,default",
            }
        )
        assert cmd[:5] == [sys.executable, "-m", "finboard_app.cli", "worker", "run"]
        assert cmd[5:] == [
            "--poll-interval",
            "0.5",
            "--max-concurrent",
            "2",
            "--queues",
            "research,default",
        ]

    def test_never_forwards_workers_flag(self) -> None:
        """--workers 不转发:子进程固定单 worker 形态,避免递归拉起进程树。"""

        from finboard_app.cli import _worker_child_command

        cmd = _worker_child_command({})
        assert "--workers" not in cmd

    def test_empty_queues_forwarded_explicitly(self) -> None:
        """显式空串 --queues(消费全部队列)原样转发,不静默丢弃。"""

        from finboard_app.cli import _worker_child_command

        assert _worker_child_command({"worker_queues": ""})[5:] == ["--queues", ""]

    def test_no_overrides_when_defaults(self) -> None:
        from finboard_app.cli import _worker_child_command

        assert _worker_child_command({}) == [
            sys.executable,
            "-m",
            "finboard_app.cli",
            "worker",
            "run",
        ]


class TestSuperviseWorkerProcesses:
    @pytest.mark.unit
    def test_all_children_exit_clean_returns_zero(self) -> None:
        from finboard_app.cli import _supervise_worker_processes

        code = _supervise_worker_processes(_long_sleep_command(1), 2)
        assert code == 0

    @pytest.mark.unit
    def test_child_failure_returns_one(self) -> None:
        from finboard_app.cli import _supervise_worker_processes

        code = _supervise_worker_processes(_exit_code_command(3), 1)
        assert code == 1

    @pytest.mark.unit
    def test_keyboard_interrupt_terminates_children(self) -> None:
        """等待循环被打断(Ctrl-C):terminate 收敛子进程并返回 0。"""

        from finboard_app import cli

        calls = {"n": 0}

        def interrupting_sleep(_: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:  # 第一轮让 Popen 有机会启动,第二轮即打断
                raise KeyboardInterrupt

        with patch("finboard_app.cli.time.sleep", side_effect=interrupting_sleep):
            code = cli._supervise_worker_processes(_long_sleep_command(30), 2)
        assert code == 0

    @pytest.mark.unit
    def test_graceful_terminate_kills_stragglers(self) -> None:
        """宽限超时后 kill 兜底:子进程拒绝退出时也能收敛。"""

        from finboard_app import cli

        # 子进程捕获 SIGTERM(POSIX)/忽略友好信号后仍会被 kill 兜底;
        # Windows TerminateProcess 本身不可拦截,直接验证收敛路径返回 0。
        stubborn = [sys.executable, "-c", "import time; time.sleep(30)"]
        calls = {"n": 0}

        def interrupting_sleep(_: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt

        with (
            patch("finboard_app.cli._WORKER_CHILD_TERMINATE_GRACE_SECONDS", 1.0),
            patch("finboard_app.cli.time.sleep", side_effect=interrupting_sleep),
        ):
            code = cli._supervise_worker_processes(stubborn, 1)
        assert code == 0


class TestWorkerRunDispatch:
    def _ctx(self) -> MagicMock:
        ctx = MagicMock()
        ctx.obj = Settings()
        return ctx

    @pytest.mark.unit
    def test_workers_gt_one_routes_to_supervisor(self) -> None:
        from finboard_app.cli import worker_run

        with (
            patch("finboard_app.cli._supervise_worker_processes", return_value=0) as sup,
            patch("finboard_app.cli.asyncio.run") as aiorun,
            pytest.raises(typer.Exit) as exc_info,
        ):
            worker_run(self._ctx(), workers=2)
        sup.assert_called_once()
        command, count = sup.call_args.args
        assert count == 2
        assert command[3:5] == ["worker", "run"]
        aiorun.assert_not_called()
        assert exc_info.value.exit_code == 0

    @pytest.mark.unit
    def test_default_single_process_unchanged(self) -> None:
        """默认路径(--workers 缺省 + worker_processes=1)零回归:进程内运行。"""

        from finboard_app.cli import worker_run

        with (
            patch("finboard_app.cli._supervise_worker_processes") as sup,
            patch("finboard_app.cli.asyncio.run") as aiorun,
        ):
            worker_run(self._ctx())
        sup.assert_not_called()
        aiorun.assert_called_once()

    @pytest.mark.unit
    def test_settings_worker_processes_gt_one_routes_to_supervisor(self) -> None:
        from finboard_app.cli import worker_run

        ctx = self._ctx()
        ctx.obj = ctx.obj.model_copy(update={"worker_processes": 3})
        with (
            patch("finboard_app.cli._supervise_worker_processes", return_value=1) as sup,
            pytest.raises(typer.Exit) as exc_info,
        ):
            worker_run(ctx)
        _, count = sup.call_args.args
        assert count == 3
        assert exc_info.value.exit_code == 1

    @pytest.mark.unit
    def test_cli_workers_option_overrides_settings(self) -> None:
        from finboard_app.cli import worker_run

        ctx = self._ctx()
        ctx.obj = ctx.obj.model_copy(update={"worker_processes": 1})
        with (
            patch("finboard_app.cli._supervise_worker_processes", return_value=0) as sup,
            pytest.raises(typer.Exit),
        ):
            worker_run(ctx, workers=4)
        _, count = sup.call_args.args
        assert count == 4
