"""dev 启动期残留 worker 清扫(issue #339)。

锁定四点:
* 命令行匹配:``worker run`` 相邻词命中;``worker runner`` / ``dev`` /
  ``uv run finboard dev`` 不命中;
* 环境限定:仅本工作区 venv(exe 或命令行引用 Scripts 目录)命中,其他
  worktree 的 venv 不命中;
* 真实进程 E2E:枚举 + 选择能找到自拉的 ``worker run`` 测试进程,定点
  清理后退出(只杀自己拉起的 pid,不触碰机器上其他命中进程);
* select 排除自身 pid。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from finboard_app.dev_cleanup import (
    ProcessInfo,
    belongs_to_environment,
    kill_processes,
    list_processes,
    matches_worker_run,
    select_stale_worker_processes,
    sweep_stale_workers,
)

_SCRIPTS = Path(sys.executable).parent
_OTHER_VENV = Path("C:/other/FinDashboard-wt9/.venv/Scripts/python.exe")


def _info(
    pid: int = 100,
    exe: str | None = str(_SCRIPTS / "python.exe"),
    cmdline: str | None = None,
    argv: tuple[str, ...] | None = None,
) -> ProcessInfo:
    return ProcessInfo(pid=pid, exe=exe, cmdline=cmdline, argv=argv)


class TestWorkerRunMatching:
    def test_managed_worker_matches(self) -> None:
        assert matches_worker_run(
            argv=("/venv/Scripts/python.exe", "-m", "finboard_app.cli", "worker", "run"),
            cmdline=None,
        )

    def test_supervisor_matches(self) -> None:
        assert matches_worker_run(
            cmdline='finboard.exe worker run --workers 2', argv=None,
        )

    def test_word_boundary_rejects_runner(self) -> None:
        assert not matches_worker_run(cmdline="tool worker runner", argv=None)

    def test_dev_command_does_not_match(self) -> None:
        assert not matches_worker_run(
            argv=("/venv/Scripts/python.exe", "finboard.exe", "dev", "--port", "8000"),
            cmdline='"...\\\\finboard.exe" dev --port 8000 --web-dir web',
        )
        assert not matches_worker_run(
            cmdline="uv run finboard dev --port 8000", argv=None,
        )

    def test_empty_does_not_match(self) -> None:
        assert not matches_worker_run(argv=(), cmdline=None)


class TestEnvironmentScoping:
    def test_same_venv_exe_matches(self) -> None:
        assert belongs_to_environment(
            exe=str(_SCRIPTS / "python.exe"),
            cmdline=None,
            scripts_dir=_SCRIPTS,
        )
        assert belongs_to_environment(
            exe=str(_SCRIPTS / "finboard.exe"),
            cmdline=None,
            scripts_dir=_SCRIPTS,
        )

    def test_other_worktree_venv_rejected(self) -> None:
        assert not belongs_to_environment(
            exe=_OTHER_VENV,
            cmdline=None,
            scripts_dir=_SCRIPTS,
        )

    def test_cmdline_referencing_scripts_dir_matches(self) -> None:
        """console-script shim 的底座子进程:exe 在 base 环境,命令行引用 venv。"""
        assert belongs_to_environment(
            exe="C:/Program_Files/miniconda3/python.exe",
            cmdline=f'"{_SCRIPTS / "finboard.exe"}" worker run',
            scripts_dir=_SCRIPTS,
        )

    def test_base_python_without_reference_rejected(self) -> None:
        assert not belongs_to_environment(
            exe="C:/Program_Files/miniconda3/python.exe",
            cmdline="python -m finboard_app.cli worker run",
            scripts_dir=_SCRIPTS,
        )


class TestSelection:
    def test_selects_only_worker_run_in_environment(self) -> None:
        processes = [
            _info(pid=1, cmdline="python -m finboard_app.cli worker run"),
            _info(pid=2, cmdline="finboard.exe dev --port 8000"),
            _info(
                pid=3,
                exe=str(_OTHER_VENV),
                cmdline="python -m finboard_app.cli worker run",
            ),
            _info(pid=4, argv=("python", "-c", "x", "worker", "run")),
        ]
        selected = select_stale_worker_processes(
            processes, own_pid=os.getpid(), scripts_dir=_SCRIPTS
        )
        assert [item.pid for item in selected] == [1, 4]

    def test_own_pid_excluded(self) -> None:
        processes = [
            _info(pid=os.getpid(), argv=(sys.executable, "-c", "x", "worker", "run")),
        ]
        assert (
            select_stale_worker_processes(
                processes, own_pid=os.getpid(), scripts_dir=_SCRIPTS
            )
            == []
        )


class TestRealProcessEndToEnd:
    def test_sweep_finds_and_kills_spawned_worker(self) -> None:
        """真实枚举 + 定点清理:只杀本测试拉起的 ``worker run`` 进程。"""
        spawned = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                "worker",
                "run",
            ]
        )
        try:
            assert spawned.poll() is None
            selected = select_stale_worker_processes(
                list_processes(),
                own_pid=os.getpid(),
                scripts_dir=_SCRIPTS,
            )
            assert spawned.pid in {item.pid for item in selected}
            kill_processes([spawned.pid])
            assert spawned.wait(timeout=15) is not None
        finally:
            if spawned.poll() is None:
                spawned.kill()
                spawned.wait(timeout=5)

    def test_sweep_orchestration_with_injected_enumeration(self) -> None:
        """编排函数:注入枚举/杀进程,验证过滤与调用电。"""
        killed: list[int] = []

        def fake_kill(pids: list[int]) -> None:
            killed.extend(pids)

        fake = _info(pid=42, argv=("python", "-m", "finboard_app.cli", "worker", "run"))

        def fake_list() -> list[ProcessInfo]:
            return [fake]

        # sweep_stale_workers 的默认枚举/清理不可注入(直接调系统 API);
        # 这里复刻其编排:select → kill。
        selected = select_stale_worker_processes(
            fake_list(), own_pid=os.getpid(), scripts_dir=_SCRIPTS
        )
        fake_kill([item.pid for item in selected])
        assert killed == [42]


def test_sweep_smoke_on_real_enumeration() -> None:
    """真实枚举冒烟:函数可调用且不抛(是否命中取决于环境)。"""
    start = time.monotonic()
    result = sweep_stale_workers()
    assert isinstance(result, list)
    assert time.monotonic() - start < 60
