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
        patch("finboard_app.cli.threading.Thread", return_value=thread),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt) as serve,
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    thread.start.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=1.0)
    serve.assert_called_once_with(ctx, host="127.0.0.1", port=8000, reload=False)


@pytest.mark.unit
def test_stop_dev_process_uses_windows_process_tree_fallback() -> None:
    """Windows 按精确 PID 清理 Vite 子进程树。"""
    from finboard_app.cli import _stop_dev_process

    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    with (
        patch("finboard_app.cli.sys.platform", "win32"),
        patch("finboard_app.cli.subprocess.run") as run,
    ):
        _stop_dev_process(process, timeout=0.01)

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
