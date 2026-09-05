"""dev 启动期残留 worker 清扫(issue #339)。

背景:``finboard dev`` 对托管 worker 的回收只在自己的 ``finally`` 里做——
dev 进程被强杀 / 终端直接关闭时,worker 子进程成为孤儿并继续消费队列。
下次 ``make dev`` 若不清场,新旧 worker(可能隔着一次代码更新)会赛跑领任务,
旧代码 worker 抢先执行就会出现「已修复的 bug 逐位复现」的假象(2026-09-05
r6/r7 复拒事故)。

枚举路径(零新依赖,双平台各一条):
* Windows —— PowerShell ``Get-CimInstance Win32_Process``(PID / 可执行路径 /
  命令行,JSON 输出);启动期一次性调用,约 1s。
* POSIX —— 遍历 ``/proc/<pid>/cmdline``(NUL 分割 argv)+ ``readlink``
  ``/proc/<pid>/exe``;macOS 无 ``/proc`` 时优雅跳过(清扫是 best-effort)。

匹配规则(双条件,防误杀):
1. 命令行含 ``worker run`` 相邻词(argv 相邻判定;Windows 仅有原始命令行
   字符串,退化为 ``worker\\s+run`` 正则 + 词边界);
2. 可执行文件位于**本工作区 venv 的 Scripts 目录**下,或命令行含该目录
   路径——后者覆盖 console-script shim(``finboard.exe worker run``)拉起的
   底座解释器子进程(其 exe 是 base python,但命令行引用 venv 内 shim)。

其他 worktree 的 venv 是不同路径字符串,不会被误杀(并行 worktree 惯例);
``finboard dev`` / pytest 自身不含 ``worker run`` 不自伤;``worker run
--workers N`` supervisor 的子进程各自命中,逐一清理。已知边缘:``-m``
启动器(venv python.exe)的 base-python 子进程若其启动器先于清扫退出,
命令行不含 venv 路径且 exe 在 base 环境——不命中,依赖启动器的树杀兜底。

处置:命中即 ``taskkill /PID /T /F``(Windows,连进程树)/ SIGKILL(POSIX),
全部具名日志;被杀任务的 job 由既有 lease 过期回收(INTERRUPTED → 退避
重排),与进程崩溃同语义。枚举失败仅警告不阻断启动。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: worker 进程的命令行特征:``worker run`` 相邻词 + 词边界
#: (``worker runner`` / ``worker run_x`` 不命中)。
_WORKER_RUN_PATTERN = re.compile(r"worker\s+run(?:[\s\"']|$)")

_POWERSHELL_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """清扫视角下的最小进程画像。"""

    pid: int
    exe: str | None
    #: Windows PowerShell 只提供原始命令行字符串;POSIX 同时给出 argv。
    cmdline: str | None
    argv: tuple[str, ...] | None


def matches_worker_run(*, argv: tuple[str, ...] | None, cmdline: str | None) -> bool:
    """命令行是否为 ``worker run`` 进程(相邻词判定)。"""

    if argv:
        return any(
            argv[index] == "worker" and argv[index + 1] == "run"
            for index in range(len(argv) - 1)
        )
    if cmdline:
        return _WORKER_RUN_PATTERN.search(cmdline) is not None
    return False


def belongs_to_environment(
    *,
    exe: str | None,
    cmdline: str | None,
    scripts_dir: Path,
) -> bool:
    """进程是否属于本工作区环境(exe 或命令行引用本 venv Scripts 目录)。

    路径比较经 :func:`os.path.normcase`(Windows 大小写不敏感)。命令行引用
    覆盖 console-script shim 的底座解释器子进程(exe 是 base python,但
    命令行含 ``<venv>/Scripts/finboard.exe worker run``)。
    """
    marker = os.path.normcase(str(scripts_dir))
    if exe and os.path.normcase(str(Path(exe).parent)) == marker:
        return True
    if not cmdline:
        return False
    return marker in os.path.normcase(cmdline)


def select_stale_worker_processes(
    processes: list[ProcessInfo],
    *,
    own_pid: int,
    scripts_dir: Path,
) -> list[ProcessInfo]:
    """从进程画像里选出应清理的本工作区残留 worker(纯函数,可测)。"""
    selected: list[ProcessInfo] = []
    for process in processes:
        if process.pid == own_pid:
            continue
        if not matches_worker_run(argv=process.argv, cmdline=process.cmdline):
            continue
        if not belongs_to_environment(
            exe=process.exe, cmdline=process.cmdline, scripts_dir=scripts_dir
        ):
            continue
        selected.append(process)
    return selected


def _list_processes_windows() -> list[ProcessInfo]:
    """PowerShell ``Get-CimInstance Win32_Process`` 枚举(单次调用)。"""
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, ExecutablePath, CommandLine | "
        "ConvertTo-Json -Compress -Depth 2"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_POWERSHELL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "dev.stale_worker_scan_failed",
            platform="win32",
            error=str(exc),
            message="worker 进程枚举失败,跳过清扫(不阻断启动)",
        )
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "dev.stale_worker_scan_failed",
            platform="win32",
            error=str(exc),
            message="worker 进程枚举输出无法解析,跳过清扫",
        )
        return []
    items = payload if isinstance(payload, list) else [payload]
    processes: list[ProcessInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(str(item.get("ProcessId", "")))
        except ValueError:
            continue
        exe = item.get("ExecutablePath")
        cmdline = item.get("CommandLine")
        processes.append(
            ProcessInfo(
                pid=pid,
                exe=str(exe) if exe else None,
                cmdline=str(cmdline) if cmdline else None,
                argv=None,
            )
        )
    return processes


def _list_processes_posix() -> list[ProcessInfo]:
    """/proc 枚举(Linux;macOS 无 /proc 时返回空,清扫优雅跳过)。"""
    proc = Path("/proc")
    if not proc.is_dir():
        logger.debug(
            "dev.stale_worker_scan_unavailable",
            platform=sys.platform,
            message="无 /proc,跳过 worker 清扫",
        )
        return []
    processes: list[ProcessInfo] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        argv = tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
        if not argv:
            continue
        processes.append(
            ProcessInfo(
                pid=int(entry.name),
                exe=exe,
                cmdline=None,
                argv=argv,
            )
        )
    return processes


def list_processes() -> list[ProcessInfo]:
    """按平台枚举进程画像;枚举失败返回空(best-effort,不阻断启动)。"""
    if sys.platform == "win32":
        return _list_processes_windows()
    return _list_processes_posix()


def kill_processes(pids: list[int]) -> None:
    """强杀指定进程(Windows 连进程树);失败逐个吞掉并记警告。"""
    for pid in pids:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "dev.stale_worker_kill_failed",
                    pid=pid,
                    returncode=result.returncode,
                )
        else:
            try:
                os.kill(pid, 9)
            except OSError as exc:
                logger.warning("dev.stale_worker_kill_failed", pid=pid, error=str(exc))


def sweep_stale_workers() -> list[ProcessInfo]:
    """清扫本工作区残留 worker,返回被清理的进程画像(best-effort)。

    由 ``finboard dev`` 在 spawn 新 worker 之前调用;``--no-worker`` 时同样
    生效(残留 worker 仍在消费本库队列)。
    """
    processes = list_processes()
    selected = select_stale_worker_processes(
        processes,
        own_pid=os.getpid(),
        scripts_dir=Path(sys.executable).parent,
    )
    if not selected:
        return []
    logger.warning(
        "dev.stale_worker_swept",
        pids=[item.pid for item in selected],
        message="发现并清理残留 worker(任务由 lease 过期回收)",
    )
    kill_processes([item.pid for item in selected])
    return selected
