"""挂死守卫验收探针(issue #167)。

两类兜底,分两个用例验证:

* **sync 挂死**(``time.sleep(999)``):pytest-timeout(thread 方式)在超时后
  dump 堆栈并 ``os._exit(1)`` 杀进程——这是进程级最后防线。用子进程验证:
  子 pytest 进程跑一个挂死测试,必须在 ~5s 内以非零退出码结束且输出
  ``Timeout``,而不是挂 999s。
* **async 挂死**(``await asyncio.sleep(999)``):pytest-timeout 的线程注入
  无法打断阻塞在 C 层(select/IOCP)的事件循环,由 tests/conftest.py 的
  ``asyncio.timeout`` 守卫兜底——守卫超时 = marker 值 - 1s,先于
  pytest-timeout 触发,给出**单测试优雅失败**(其余测试继续)。本用例直接
  挂死并预期 xfail;若守卫失效,进程会被 pytest-timeout 在 5s 后杀掉
  (os._exit,无 summary)—— 也是可见失败,但会中断后续测试。

验收运行(默认被 ``addopts -m "not hang_guard"`` 排除):

    uv run pytest tests/unit/test_hang_guard.py -m hang_guard

预期:2 个用例 ~10s 内结束,1 xfailed + 1 passed(sync 用例是子进程断言)。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.hang_guard]

_SYNC_PROBE = textwrap.dedent(
    """\
    import time

    def test_sync_hang():
        time.sleep(999)
    """
)


@pytest.mark.timeout(30)
def test_sync_hang_kills_process() -> None:
    """sync 挂死必须在超时后杀掉 pytest 进程(进程级兜底生效)。"""
    import shutil
    import tempfile

    # 不用 pytest 的 tmp_path fixture:Windows 上 Temp 基目录 ACL 损坏时
    # fixture 直接 PermissionError,探针用 tempfile 自建更稳。
    workdir = Path(tempfile.mkdtemp(prefix="hang-guard-"))
    try:
        probe_file = workdir / "probe_sync_hang.py"
        probe_file.write_text(_SYNC_PROBE, encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe_file),
                "--timeout=5",
                "--timeout-method=thread",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "-q",
            ],
            capture_output=True,
            text=True,
            timeout=25,  # 子进程若 5s 兜底失效,这里兜底(25s 后断言失败)
            cwd=workdir,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode != 0, (
            "挂死测试未被 pytest-timeout 杀掉,子进程正常退出\n" + output
        )
        assert "Timeout" in output, (
            "子进程退出但未见 Timeout 转储\n" + output
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.mark.xfail(strict=True, reason="挂死探针:预期被 asyncio.timeout 守卫打断")
@pytest.mark.timeout(5)
async def test_async_hang_fails_gracefully() -> None:
    """async 挂死由 conftest 守卫优雅打断(4s 后 TimeoutError,进程继续)。"""
    import asyncio

    await asyncio.sleep(999)
