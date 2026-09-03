"""开发服务器进程监管测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
def test_dev_keeps_api_in_foreground_and_always_stops_frontend(tmp_path: Path) -> None:
    """后端不得作为 shell 后台孙进程;退出时必须回收 Vite。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = MagicMock()
    thread = MagicMock()

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker") as spawn_worker,
        patch("finboard_app.cli._stop_dev_process"),
        patch("finboard_app.cli.threading.Thread", return_value=thread),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt) as serve,
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    thread.start.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=1.0)
    serve.assert_called_once_with(ctx, host="127.0.0.1", port=8000, reload=False)
    spawn_worker.assert_called_once_with()


@pytest.mark.unit
def test_dev_no_worker_skips_spawn(tmp_path: Path) -> None:
    """--no-worker 逃生开关:dev 不启动后台 worker(生产独立部署形态)。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = MagicMock()
    thread = MagicMock()

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker") as spawn_worker,
        patch("finboard_app.cli.threading.Thread", return_value=thread),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path, no_worker=True)

    spawn_worker.assert_not_called()


@pytest.mark.unit
def test_dev_worker_process_is_stopped_on_exit(tmp_path: Path) -> None:
    """dev 退出时必须与 Vite 一样回收 worker 子进程(不留孤儿进程)。"""
    from finboard_app.cli import _WORKER_STOP_FALLBACK_GRACE_SECONDS, dev
    from finboard_app.config import Settings

    ctx = MagicMock()
    # settings 必须是真实 Settings:worker 停机宽限(#307)从 settings 读取。
    ctx.obj = Settings()
    thread = MagicMock()
    worker_process = MagicMock(spec=subprocess.Popen)

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker", return_value=worker_process),
        patch("finboard_app.cli.threading.Thread", return_value=thread),
        patch("finboard_app.cli._stop_dev_process") as stop_process,
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    # worker 停机走 #307 优雅路径:settings 宽限默认 0 → dev 给 10s 兜底窗口
    # (先 CTRL_BREAK/SIGTERM 后强杀);Vite 仍走默认参数。
    stop_process.assert_any_call(
        worker_process,
        graceful_seconds=_WORKER_STOP_FALLBACK_GRACE_SECONDS,
    )
    # 本用例前端线程被 mock(frontend_holder 为空),只发生 worker 这一次回收。
    assert stop_process.call_count == 1


@pytest.mark.unit
def test_dev_worker_command_uses_module_entry() -> None:
    """dev 托管的 worker 用同一 venv python 跑 finboard_app.cli,与 CLI 行为一致。"""
    import sys

    from finboard_app.cli import _dev_worker_command

    cmd = _dev_worker_command()
    assert cmd[0] == sys.executable
    assert cmd[-4:] == ["-m", "finboard_app.cli", "worker", "run"]


@pytest.mark.unit
def test_stop_dev_process_kills_real_child() -> None:
    """真实子进程验证:stop 后进程树退出,不留下孤儿进程(Windows + Unix)。

    用无害的 sleep 子进程验证回收逻辑,避免在测试期间拉起真实 worker
    连接开发数据库。子进程按 dev/supervisor 相同的进程组形态托管:
    graceful>0 先发优雅信号(Windows CTRL_BREAK / POSIX SIGTERM),子进程
    自行退出,不强杀(issue #307)。
    """
    import subprocess as sp
    import sys
    import time

    from finboard_app.cli import _stop_dev_process

    if sys.platform == "win32":
        process = sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=sp.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        process = sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
    try:
        _stop_dev_process(process, graceful_seconds=10.0)
    finally:
        if process.poll() is None:
            process.kill()
    for _ in range(100):
        if process.poll() is not None:
            break
        time.sleep(0.05)
    assert process.poll() is not None


@pytest.mark.unit
def test_stop_dev_process_uses_windows_process_tree_fallback() -> None:
    """Windows graceful=0(或优雅超时)按精确 PID 清理整棵子进程树。"""
    from finboard_app.cli import _stop_dev_process

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


@pytest.mark.unit
def test_dev_does_not_start_frontend_when_database_is_unavailable(tmp_path: Path) -> None:
    """数据库预检失败时直接退出,不得留下 Vite 代理错误。"""
    import typer

    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = MagicMock()
    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", side_effect=TimeoutError),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli.threading.Thread") as thread,
        pytest.raises(typer.Exit) as exc_info,
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    assert exc_info.value.exit_code == 1
    thread.assert_not_called()


@pytest.mark.unit
def test_ensure_dev_database_wakes_wsl_and_retries() -> None:
    """首次连接失败时唤醒 WSL PostgreSQL,成功后再次验证数据库。"""
    from finboard_app.cli import _ensure_dev_database

    settings = MagicMock()
    check = AsyncMock(side_effect=[TimeoutError, None])
    with (
        patch("finboard_app.cli._check_dev_database", new=check),
        patch("finboard_app.cli._wake_wsl_postgresql", return_value=True) as wake,
    ):
        assert _ensure_dev_database(settings) is True

    assert check.await_count == 2
    wake.assert_called_once_with(settings)


@pytest.mark.unit
def test_wake_wsl_postgresql_starts_fixed_local_service() -> None:
    """自动唤醒仅针对本机数据库,并使用固定的 PostgreSQL 服务名。"""
    from finboard_app.cli import _wake_wsl_postgresql

    settings = MagicMock()
    settings.db_url = "postgresql+psycopg://user:secret@127.0.0.1:5432/db"
    completed = MagicMock(returncode=0)
    with (
        patch("finboard_app.cli.sys.platform", "win32"),
        patch("finboard_app.cli.subprocess.run", return_value=completed) as run,
    ):
        assert _wake_wsl_postgresql(settings) is True

    args = run.call_args.args[0]
    assert args[:6] == ["wsl.exe", "-u", "root", "-e", "sh", "-lc"]
    assert "systemctl start postgresql" in args[6]
    assert "pg_isready -h 127.0.0.1 -p 5432" in args[6]
    assert "secret" not in " ".join(args)


# --------------------------------------------------------------------------- #
# 沙箱镜像 dev 预检(issue #240)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_ensure_sandbox_image_skips_when_disabled() -> None:
    """开关关闭(默认)零 docker 调用,行为与历史版本一致。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = False
    with patch("finboard_app.cli.subprocess.run") as run:
        assert _ensure_sandbox_image(settings) is False
    run.assert_not_called()


@pytest.mark.unit
def test_ensure_sandbox_image_noop_when_image_present() -> None:
    """镜像已存在只做一次 inspect,不构建。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = True
    settings.research_sandbox_docker_bin = "docker"
    settings.research_sandbox_image = "finboard-research-sandbox:0.2.0"
    with patch(
        "finboard_app.cli.subprocess.run",
        return_value=MagicMock(returncode=0),
    ) as run:
        assert _ensure_sandbox_image(settings) is False
    assert run.call_count == 1
    assert run.call_args.args[0][:3] == ["docker", "image", "inspect"]


@pytest.mark.unit
def test_ensure_sandbox_image_builds_when_missing() -> None:
    """镜像缺失时用与 CI 同源的 Dockerfile 自动构建。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = True
    settings.research_sandbox_docker_bin = "docker"
    settings.research_sandbox_image = "finboard-research-sandbox:0.2.0"
    results = [MagicMock(returncode=1), MagicMock(returncode=0)]
    with patch("finboard_app.cli.subprocess.run", side_effect=results) as run:
        assert _ensure_sandbox_image(settings) is True
    assert run.call_count == 2
    build_cmd = run.call_args.args[0]
    assert build_cmd[0:2] == ["docker", "build"]
    assert "docker/research-sandbox/Dockerfile" in build_cmd
    assert "finboard-research-sandbox:0.2.0" in build_cmd


@pytest.mark.unit
def test_ensure_sandbox_image_build_failure_warns_not_raises() -> None:
    """构建失败仅警告返回 False,不阻断 dev 启动。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = True
    settings.research_sandbox_docker_bin = "docker"
    settings.research_sandbox_image = "finboard-research-sandbox:0.2.0"
    results = [MagicMock(returncode=1), MagicMock(returncode=2)]
    with patch("finboard_app.cli.subprocess.run", side_effect=results):
        assert _ensure_sandbox_image(settings) is False


@pytest.mark.unit
def test_ensure_sandbox_image_docker_missing_warns_not_raises() -> None:
    """docker CLI 缺失(OSError)警告跳过。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = True
    settings.research_sandbox_docker_bin = "docker"
    settings.research_sandbox_image = "finboard-research-sandbox:0.2.0"
    with patch(
        "finboard_app.cli.subprocess.run", side_effect=FileNotFoundError("docker")
    ):
        assert _ensure_sandbox_image(settings) is False

