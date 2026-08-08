"""OpenCodeProcessManager / OpenCodeProcessConfig 单元测试(issue #118)。

重点验证:
* 启动命令构造(port/hostname/cors);
* 子进程环境变量严格白名单 —— FinBoard 的 DB 密码 / broker 凭证 / API Key 不泄露;
* 凭证生成(空密码自动生成强随机);
* 健康探测 / 状态快照(用 httpx MockTransport,不启动真实进程)。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from finboard_opencode.process_manager import (
    OpenCodeProcessConfig,
    OpenCodeProcessError,
    OpenCodeProcessManager,
    _extract_version,
    _generate_password,
    which_opencode,
)

#: 预批准的临时目录(见 AGENTS.md),避免 pytest tmp_path 在 Windows 的权限问题。
_TMP_ROOT = Path(tempfile.gettempdir()) / "opencode-test"


@pytest.fixture
def work_tmp() -> Path:
    """每个测试独立的临时目录(位于预批准的 temp/opencode-test 下)。"""
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="oc-pm-", dir=str(_TMP_ROOT)))

# ---------------------------------------------------------------------------
# OpenCodeProcessConfig
# ---------------------------------------------------------------------------


def test_build_command_includes_port_hostname_cors() -> None:
    config = OpenCodeProcessConfig(
        binary="/usr/local/bin/opencode",
        port=4097,
        hostname="127.0.0.1",
        cors_origins=["http://localhost:5173", "http://localhost:8000"],
    )
    cmd = config.build_command()
    assert cmd[0] == "/usr/local/bin/opencode"
    assert cmd[1] == "web"
    assert "--port" in cmd
    assert "4097" in cmd
    assert "--hostname" in cmd
    assert "127.0.0.1" in cmd
    assert "--cors" in cmd
    cors_idx = cmd.index("--cors")
    assert cmd[cors_idx + 1] == "http://localhost:5173"
    assert cmd[cors_idx + 2] == "http://localhost:8000"


def test_build_command_omits_empty_cors() -> None:
    config = OpenCodeProcessConfig(cors_origins=[])
    cmd = config.build_command()
    assert "--cors" not in cmd


def test_base_url() -> None:
    config = OpenCodeProcessConfig(port=4097, hostname="127.0.0.1")
    assert config.base_url == "http://127.0.0.1:4097"


def test_build_environment_strict_whitelist_no_leak() -> None:
    """严格白名单:FinBoard 的 DB 密码 / broker 凭证 / API Key 不得进入子进程。"""
    parent_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "FINBOARD_DB_URL": "postgresql://secret:supersecret@host/db",
        "FINBOARD_QMT_PASSWORD": "broker-secret",
        "FINBOARD_LLM_API_KEY": "sk-super-secret",
        "OPENAI_API_KEY": "sk-leak",
    }
    config = OpenCodeProcessConfig(
        username="opencode", password="cfg-password"
    )
    env = config.build_environment(parent_env=parent_env)
    # 继承白名单。
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"
    # OpenCode basic auth 注入。
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["OPENCODE_SERVER_PASSWORD"] == "cfg-password"
    # 敏感字段绝不泄露。
    assert "FINBOARD_DB_URL" not in env
    assert "FINBOARD_QMT_PASSWORD" not in env
    assert "FINBOARD_LLM_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_environment_env_overrides_win_whitelist() -> None:
    """显式 env_overrides 优先级高于继承,用于注入 LLM provider Key。"""
    config = OpenCodeProcessConfig(
        env_overrides={"ANTHROPIC_API_KEY": "sk-llm-key"},
    )
    env = config.build_environment(parent_env={"PATH": "/usr/bin"})
    assert env["ANTHROPIC_API_KEY"] == "sk-llm-key"
    assert env["OPENCODE_SERVER_PASSWORD"]  # 自动生成占位符


def test_resolved_password_placeholder_when_empty() -> None:
    config = OpenCodeProcessConfig(password="")
    # 配置层只暴露占位符;真实密码由 ProcessManager.start 生成。
    assert config.resolved_password


# ---------------------------------------------------------------------------
# OpenCodeProcessManager
# ---------------------------------------------------------------------------


@pytest.fixture
def unmanaged_manager() -> OpenCodeProcessManager:
    """非托管管理器(不启动子进程,用于测试健康探测 / 状态)。"""
    config = OpenCodeProcessConfig(
        port=4097, hostname="127.0.0.1", password="test-pass"
    )
    return OpenCodeProcessManager(config, manage_process=False)


def _inject_mock_transport(
    manager: OpenCodeProcessManager, handler
) -> None:
    manager._health_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=manager.base_url,
    )


async def test_effective_password_uses_config(unmanaged_manager) -> None:
    assert unmanaged_manager.effective_password == "test-pass"


async def test_effective_password_autogenerate_when_empty() -> None:
    config = OpenCodeProcessConfig(password="")
    manager = OpenCodeProcessManager(config, manage_process=False)
    assert manager.effective_password
    assert manager.effective_password != ""


async def test_health_returns_dict_when_ok(unmanaged_manager) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/global/health"
        return httpx.Response(200, json={"healthy": True, "version": "1.18.15"})

    _inject_mock_transport(unmanaged_manager, handler)
    data = await unmanaged_manager.health()
    assert data is not None
    assert data["version"] == "1.18.15"


async def test_health_returns_none_on_http_error(unmanaged_manager) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    _inject_mock_transport(unmanaged_manager, handler)
    assert await unmanaged_manager.health() is None


async def test_health_returns_none_on_network_error(unmanaged_manager) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _inject_mock_transport(unmanaged_manager, handler)
    assert await unmanaged_manager.health() is None


async def test_status_snapshot_unmanaged(unmanaged_manager) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1.18.15"})

    _inject_mock_transport(unmanaged_manager, handler)
    status = await unmanaged_manager.status()
    assert status.running is True
    assert status.managed is False
    assert status.pid is None
    assert status.base_url == "http://127.0.0.1:4097"
    assert status.healthy is True
    assert status.version == "1.18.15"


async def test_status_healthy_false_when_down(unmanaged_manager) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _inject_mock_transport(unmanaged_manager, handler)
    status = await unmanaged_manager.status()
    assert status.running is True  # 非托管始终视为运行
    assert status.healthy is False


async def test_unmanaged_start_is_noop(unmanaged_manager) -> None:
    # 非托管模式 start 不启动子进程。
    await unmanaged_manager.start()
    assert unmanaged_manager._proc is None


async def test_managed_start_requires_binary(work_tmp, monkeypatch) -> None:
    """托管模式 binary 不存在时报 OpenCodeProcessError(用 mock,避免真实子进程)。"""
    config = OpenCodeProcessConfig(
        binary="/nonexistent/opencode-binary-xyz",
        workdir=str(work_tmp / "ws"),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)

    async def _raise_fnf(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)
    with pytest.raises(OpenCodeProcessError, match="binary not found"):
        await manager.start()


class _FakeRunningProc:
    """伪造运行中进程(returncode=None),用于测试 is_running / 双重启动守卫。"""

    pid = 99999
    returncode: int | None = None


async def test_managed_double_start_raises(work_tmp) -> None:
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp / "ws"),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    manager._proc = _FakeRunningProc()  # type: ignore[assignment]
    with pytest.raises(OpenCodeProcessError, match="already running"):
        await manager.start()


class _FakeStoppableProc:
    """伪造可终止的进程:terminate() 改 returncode,wait() 立即返回。"""

    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1

    async def wait(self) -> int:
        return self.returncode or 0


async def test_managed_stop_terminates_process(work_tmp) -> None:
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp / "ws"),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    fake_proc = _FakeStoppableProc()
    manager._proc = fake_proc  # type: ignore[assignment]
    await manager.stop(grace_seconds=3.0)
    assert manager._proc is None
    assert fake_proc.terminated is True


async def test_managed_stop_force_kills_on_timeout(work_tmp, monkeypatch) -> None:
    """grace 超时后升级为 SIGKILL。"""
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp / "ws"),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    fake_proc = _FakeStoppableProc()

    async def _never_returns() -> int:
        await asyncio.sleep(100)
        return 0

    monkeypatch.setattr(fake_proc, "wait", _never_returns)
    manager._proc = fake_proc  # type: ignore[assignment]
    await manager.stop(grace_seconds=0.1)
    assert manager._proc is None
    assert fake_proc.killed is True


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_extract_version_various_keys() -> None:
    assert _extract_version({"version": "1.18.15"}) == "1.18.15"
    assert _extract_version({"Version": "2.0"}) == "2.0"
    assert _extract_version({"server_version": "3"}) == "3"
    assert _extract_version({"other": "x"}) is None
    assert _extract_version("not-a-dict") is None


def test_generate_password_is_strong() -> None:
    pw = _generate_password()
    assert len(pw) >= 16
    assert pw != _generate_password()  # 随机性


def test_which_opencode_returns_none_for_missing() -> None:
    assert which_opencode("definitely-not-a-real-binary-xyz123") is None


def test_which_opencode_finds_python() -> None:
    # python 几乎一定在 PATH 里。
    assert which_opencode("python") is not None or which_opencode("python3") is not None
