"""开发服务器进程监管测试。"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from finboard_app.config import Settings


def _settings(**overrides: Any) -> Settings:
    """测试用 Settings:切断仓库 .env 依赖(host/port/开关全显式)。"""
    from finboard_app.config import Settings as _Settings

    base: dict[str, Any] = {
        "mcp_enabled": True,
        "mcp_transport": "streamable-http",
        "mcp_host": "127.0.0.1",
        "mcp_port": 18766,
        "worker_shutdown_grace_seconds": 0,
    }
    base.update(overrides)
    # pydantic-settings 的 _env_file 不在类型签名里(Settings 运行时注入)。
    return _Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.mark.unit
def test_dev_keeps_api_in_foreground_and_always_stops_frontend(tmp_path: Path) -> None:
    """后端不得作为 shell 后台孙进程;退出时必须回收 Vite。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    # issue #544:settings 必须显式关掉 MCP 与 `.env` 读取,否则会真的拉起子进程。
    ctx.obj = _settings(mcp_enabled=False)
    threads: list[tuple[MagicMock, dict[str, object]]] = []

    def _thread_factory(*_: object, **kwargs: object) -> MagicMock:
        thread = MagicMock()
        threads.append((thread, kwargs))
        return thread

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker") as spawn_worker,
        patch("finboard_app.cli._stop_dev_process"),
        patch("finboard_app.cli.threading.Thread", side_effect=_thread_factory),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt) as serve,
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    frontend_thread = next(
        thread
        for thread, kwargs in threads
        if kwargs.get("name") == "finboard-vite-launcher"
    )
    frontend_thread.start.assert_called_once_with()
    frontend_thread.join.assert_called_once_with(timeout=1.0)
    serve.assert_called_once_with(ctx, host="127.0.0.1", port=8000, reload=False)
    spawn_worker.assert_called_once_with()


@pytest.mark.unit
def test_dev_no_worker_skips_spawn(tmp_path: Path) -> None:
    """--no-worker 逃生开关:dev 不启动后台 worker(生产独立部署形态)。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = _settings(mcp_enabled=False)
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

    ctx = MagicMock()
    # settings 必须是真实 Settings:worker 停机宽限(#307)从 settings 读取。
    # issue #544 起 dev 还会托管 MCP,本用例只关注 worker → 关掉 MCP 保持隔离。
    ctx.obj = _settings(mcp_enabled=False)
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


# --------------------------------------------------------------------------- #
# dev 托管 MCP 研究服务(issue #544)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_should_spawn_dev_mcp_matrix() -> None:
    """只有「开关打开 + 非 stdio 传输」才由 dev 托管;stdio 由客户端自己 spawn。"""
    from finboard_app.cli import _should_spawn_dev_mcp

    assert _should_spawn_dev_mcp(_settings())
    # streamable-http / sse 都是常驻 HTTP 服务,都能托管。
    assert _should_spawn_dev_mcp(_settings(mcp_transport="sse"))
    # 多 worktree 场景:脚本默认关掉 MCP,dev 必须尊重该开关,否则抢 8765。
    assert not _should_spawn_dev_mcp(_settings(mcp_enabled=False))
    # stdio 形态由宿主/客户端拉起,dev 再起一个没有消费者。
    assert not _should_spawn_dev_mcp(_settings(mcp_transport="stdio"))


@pytest.mark.unit
def test_dev_mcp_command_uses_module_entry() -> None:
    """MCP 子进程用同一 venv python 跑 ``python -m finboard_mcp``。"""
    import sys

    from finboard_app.cli import _dev_mcp_command

    assert _dev_mcp_command() == [sys.executable, "-m", "finboard_mcp"]


@pytest.mark.unit
def test_start_dev_mcp_spawns_without_waiting() -> None:
    """``_start_dev_mcp`` 只做前置检查 + spawn:长等待在旁路线程,不在 API 关键路径。"""
    from finboard_app.cli import _start_dev_mcp

    process = MagicMock(spec=subprocess.Popen)
    with (
        patch("finboard_app.cli._dev_mcp_port_in_use", return_value=False),
        patch("finboard_app.cli._spawn_dev_mcp", return_value=process) as spawn,
    ):
        assert _start_dev_mcp(_settings()) is process

    spawn.assert_called_once_with()


@pytest.mark.unit
def test_start_dev_mcp_skips_non_loopback_bind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非 loopback 绑定不随 dev 托管:远程暴露必须显式走 mcp-serve-http-remote。"""
    from finboard_app.cli import _start_dev_mcp

    with patch("finboard_app.cli._spawn_dev_mcp") as spawn:
        assert _start_dev_mcp(_settings(mcp_host="0.0.0.0")) is None

    spawn.assert_not_called()
    assert "非 loopback" in capsys.readouterr().err


@pytest.mark.unit
def test_start_dev_mcp_skips_when_port_in_use(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """端口已被占用(上次 dev 残留 / 手动实例)时不假启动,并点名归属。"""
    from finboard_app.cli import _start_dev_mcp

    with (
        patch("finboard_app.cli._dev_mcp_port_in_use", return_value=True),
        patch("finboard_app.cli._spawn_dev_mcp") as spawn,
    ):
        assert _start_dev_mcp(_settings()) is None

    spawn.assert_not_called()
    assert "已被占用" in capsys.readouterr().err


@pytest.mark.unit
def test_start_dev_mcp_warns_when_spawn_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令缺失等 Popen 失败只告警。"""
    from finboard_app.cli import _start_dev_mcp

    with (
        patch("finboard_app.cli._dev_mcp_port_in_use", return_value=False),
        patch("finboard_app.cli._spawn_dev_mcp", side_effect=OSError("no uv")),
    ):
        assert _start_dev_mcp(_settings()) is None

    assert "MCP 研究服务启动失败" in capsys.readouterr().err


@pytest.mark.unit
def test_watch_dev_mcp_reports_listening_port(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """就绪后打印真实 host:port 与 --no-mcp 提示(AC 可断言)。"""
    from finboard_app.cli import _watch_dev_mcp

    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    with patch("finboard_app.cli._dev_mcp_port_in_use", return_value=True):
        _watch_dev_mcp(process, _settings(), threading.Event())

    out = capsys.readouterr().out
    assert "127.0.0.1:18766" in out
    assert "--no-mcp" in out


@pytest.mark.unit
def test_watch_dev_mcp_warns_when_child_exits_immediately(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """子进程早退(端口被占 / 无凭据退出码 2)必须点名,而不是打印成功。"""
    from finboard_app.cli import _watch_dev_mcp

    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = 2
    process.returncode = 2
    _watch_dev_mcp(process, _settings(), threading.Event())

    assert "启动即退出(退出码 2)" in capsys.readouterr().err


@pytest.mark.unit
def test_watch_dev_mcp_warns_when_listen_times_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """有界等待:一直不监听只告警(进程仍由调用方回收)。"""
    from finboard_app.cli import _watch_dev_mcp

    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = None
    with (
        patch("finboard_app.cli._dev_mcp_port_in_use", return_value=False),
        patch("finboard_app.cli._DEV_MCP_READY_TIMEOUT_SECONDS", 0.0),
    ):
        _watch_dev_mcp(process, _settings(), threading.Event())

    assert "未监听" in capsys.readouterr().err


@pytest.mark.unit
def test_watch_dev_mcp_silent_when_stopping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """主动停机时不得把 uvicorn 的优雅退出码 3 打印成「启动即退出」的假故障。"""
    from finboard_app.cli import _watch_dev_mcp

    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = 3
    process.returncode = 3
    stop_event = threading.Event()
    stop_event.set()
    _watch_dev_mcp(process, _settings(), stop_event)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.unit
def test_dev_spawns_mcp_and_watches_it_in_background(tmp_path: Path) -> None:
    """spawn 后立刻起 daemon 就绪线程;主线程继续跑 API,退出时回收 MCP。"""
    from finboard_app.cli import _watch_dev_mcp, dev

    ctx = MagicMock()
    ctx.obj = _settings()
    mcp_process = MagicMock(spec=subprocess.Popen)
    created: list[dict[str, Any]] = []

    def _factory(*args: Any, **kwargs: Any) -> MagicMock:
        thread = MagicMock()
        created.append({"args": args, "kwargs": kwargs, "thread": thread})
        return thread

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker", return_value=None),
        patch("finboard_app.cli._start_dev_mcp", return_value=mcp_process) as start_mcp,
        patch("finboard_app.cli.threading.Thread", side_effect=_factory),
        patch("finboard_app.cli._stop_dev_process") as stop_process,
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    start_mcp.assert_called_once()
    readiness = next(
        item for item in created if item["kwargs"].get("name") == "finboard-mcp-readiness"
    )
    readiness["thread"].start.assert_called_once_with()
    assert readiness["kwargs"]["target"] is _watch_dev_mcp
    args = readiness["kwargs"]["args"]
    assert args[:2] == (mcp_process, ctx.obj)
    assert isinstance(args[2], threading.Event)
    readiness["thread"].join.assert_called_once_with(timeout=0.5)
    assert readiness["kwargs"]["daemon"] is True
    stop_process.assert_any_call(mcp_process)


@pytest.mark.unit
def test_dev_no_mcp_skips_spawn(tmp_path: Path) -> None:
    """--no-mcp 逃生开关:dev 不启动 MCP(客户端自己用 make mcp-serve-http)。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = _settings()

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker", return_value=None),
        patch("finboard_app.cli._start_dev_mcp") as start_mcp,
        patch("finboard_app.cli.threading.Thread", return_value=MagicMock()),
        patch("finboard_app.cli._stop_dev_process"),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path, no_mcp=True)

    start_mcp.assert_not_called()


@pytest.mark.unit
def test_dev_stdio_transport_skips_mcp_spawn(tmp_path: Path) -> None:
    """stdio 形态由宿主/客户端拉起,dev 不代管。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = _settings(mcp_transport="stdio")

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker", return_value=None),
        patch("finboard_app.cli._start_dev_mcp") as start_mcp,
        patch("finboard_app.cli.threading.Thread", return_value=MagicMock()),
        patch("finboard_app.cli._stop_dev_process"),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    start_mcp.assert_not_called()


@pytest.mark.unit
def test_dev_second_interrupt_still_stops_remaining_processes(tmp_path: Path) -> None:
    """第二个进程的回收不能因为第一个回收被二次 Ctrl-C 打断而跳过。"""
    from finboard_app.cli import dev

    ctx = MagicMock()
    ctx.obj = _settings()
    mcp_process = MagicMock(spec=subprocess.Popen)
    worker_process = MagicMock(spec=subprocess.Popen)
    stopped: list[object] = []

    def _stop(process: subprocess.Popen[bytes], *, graceful_seconds: float = 5.0) -> None:
        stopped.append(process)
        if process is mcp_process:
            raise KeyboardInterrupt

    with (
        patch("finboard_app.cli.shutil.which", return_value="npm.cmd"),
        patch("finboard_app.cli._ensure_dev_database", return_value=False),
        patch("finboard_app.cli._ensure_sandbox_image", return_value=False),
        patch("finboard_app.cli._spawn_dev_worker", return_value=worker_process),
        patch("finboard_app.cli._start_dev_mcp", return_value=mcp_process),
        patch("finboard_app.cli.threading.Thread", return_value=MagicMock()),
        patch("finboard_app.cli._stop_dev_process", side_effect=_stop),
        patch("finboard_app.cli.serve", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        dev(ctx, host="127.0.0.1", port=8000, web_dir=tmp_path)

    assert stopped == [mcp_process, worker_process]


@pytest.mark.unit
def test_stop_dev_process_force_kills_after_second_interrupt_windows() -> None:
    """二次 Ctrl-C 打断等待时,仍必须走 taskkill /T /F 强杀整树,不能只发信号。"""
    from finboard_app.cli import _stop_dev_process

    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.poll.return_value = None
    process.wait.side_effect = KeyboardInterrupt
    with (
        patch("finboard_app.cli.sys.platform", "win32"),
        patch("finboard_app.cli.os.kill"),
        patch("finboard_app.cli.subprocess.run") as run,
    ):
        _stop_dev_process(process, graceful_seconds=5.0)

    run.assert_called_once_with(
        ["taskkill", "/PID", "12345", "/T", "/F"],
        check=False,
        capture_output=True,
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX 专用分支:Windows 无 os.killpg / signal.SIGKILL(由 Linux CI 覆盖)",
)
@pytest.mark.unit
def test_stop_dev_process_escalates_to_sigkill_after_second_interrupt() -> None:
    """POSIX:等待被打断后升到 SIGKILL,而不是停在 SIGTERM。"""
    import signal as signal_module

    from finboard_app.cli import _stop_dev_process

    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.poll.return_value = None
    process.wait.side_effect = [KeyboardInterrupt, None]
    with (
        patch("finboard_app.cli.sys.platform", "linux"),
        patch("finboard_app.cli.os.killpg") as killpg,
    ):
        _stop_dev_process(process, graceful_seconds=5.0)

    sent = [call.args[1] for call in killpg.call_args_list]
    # Windows 上没有 signal.SIGKILL(本用例在 win32 被 skip),用 getattr 兜住类型检查。
    assert getattr(signal_module, "SIGKILL", 9) in sent


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
    settings.research_sandbox_image = "finboard-research-sandbox:0.3.0"
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
    settings.research_sandbox_image = "finboard-research-sandbox:0.3.0"
    results = [MagicMock(returncode=1), MagicMock(returncode=0)]
    with patch("finboard_app.cli.subprocess.run", side_effect=results) as run:
        assert _ensure_sandbox_image(settings) is True
    assert run.call_count == 2
    build_cmd = run.call_args.args[0]
    assert build_cmd[0:2] == ["docker", "build"]
    assert "docker/research-sandbox/Dockerfile" in build_cmd
    assert "finboard-research-sandbox:0.3.0" in build_cmd


@pytest.mark.unit
def test_ensure_sandbox_image_build_failure_warns_not_raises() -> None:
    """构建失败仅警告返回 False,不阻断 dev 启动。"""
    from finboard_app.cli import _ensure_sandbox_image

    settings = MagicMock()
    settings.research_sandbox_enabled = True
    settings.research_sandbox_docker_bin = "docker"
    settings.research_sandbox_image = "finboard-research-sandbox:0.3.0"
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
    settings.research_sandbox_image = "finboard-research-sandbox:0.3.0"
    with patch(
        "finboard_app.cli.subprocess.run", side_effect=FileNotFoundError("docker")
    ):
        assert _ensure_sandbox_image(settings) is False
