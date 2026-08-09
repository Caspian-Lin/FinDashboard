"""OpenCodeProcessManager / OpenCodeProcessConfig 单元测试(issue #xxx Docker 隔离)。

重点验证:
* ``docker run`` 命令构造(image/container_name/port/hostname/cors/mounts/env);
* docker CLI 子进程环境变量严格白名单 —— FinBoard 的 DB 密码 / broker 凭证不泄露;
* 凭证生成(空密码自动生成强随机);
* 健康探测 / 状态快照(用 httpx MockTransport,不启动真实容器);
* docker CLI 不可用时 start() 报 OpenCodeProcessError(降级,不炸 lifespan)。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from finboard_opencode.process_manager import (
    OpenCodeProcessConfig,
    OpenCodeProcessError,
    OpenCodeProcessManager,
    _docker_container_running,
    _extract_version,
    _generate_password,
    _resolve_docker_binary,
    _windows_docker_binary_candidates,
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
# OpenCodeProcessConfig — docker run 命令构造
# ---------------------------------------------------------------------------


def test_build_docker_run_command_basic() -> None:
    config = OpenCodeProcessConfig(
        image="ghcr.io/anomalyco/opencode:latest",
        container_name="test-oc",
        port=4097,
        cors_origins=["http://localhost:5173"],
        username="opencode",
        password="cfg-pwd",
        mcp_auth_token="mcp-token",
        env_overrides={"DEEPSEEK_API_KEY": "sk-123"},
        workdir=".",
    )
    cmd = config.build_docker_run_command(effective_password="cfg-pwd")
    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "-d" in cmd
    assert "--name" in cmd
    assert "test-oc" in cmd
    assert "--add-host=host.docker.internal:host-gateway" in cmd
    # 端口映射:宿主机侧 127.0.0.1 锁 loopback。
    assert "-p" in cmd
    assert "127.0.0.1:4097:4097" in cmd
    # 镜像 + 子命令。
    assert "ghcr.io/anomalyco/opencode:latest" in cmd
    assert "web" in cmd
    # 容器内绑 0.0.0.0(端口映射要求),宿主机侧由 -p 锁 loopback。
    assert "0.0.0.0" in cmd
    assert "--port" in cmd
    assert "4097" in cmd
    # bind mount .opencode / .agents。
    assert any("/workspace/.opencode" in p for p in cmd)
    assert any("/workspace/.agents" in p for p in cmd)
    # named volume 持久化会话 DB / auth(容器删除后保留)。
    assert "opencode-data:/root/.local/share/opencode" in cmd
    assert "opencode-config:/root/.config/opencode" in cmd
    # basic auth 凭证 -e。
    assert any(p == "OPENCODE_SERVER_USERNAME=opencode" for p in cmd)
    assert any(p == "OPENCODE_SERVER_PASSWORD=cfg-pwd" for p in cmd)
    # MCP token。
    assert any(p == "FINBOARD_MCP_TOKEN=mcp-token" for p in cmd)
    # env_overrides。
    assert any(p == "DEEPSEEK_API_KEY=sk-123" for p in cmd)
    # CORS。
    cors_idx = cmd.index("--cors")
    assert cmd[cors_idx + 1] == "http://localhost:5173"


def test_build_docker_run_command_omits_empty_cors() -> None:
    config = OpenCodeProcessConfig(cors_origins=[])
    cmd = config.build_docker_run_command(effective_password="p")
    assert "--cors" not in cmd


def test_build_docker_run_command_omits_mcp_token_when_empty() -> None:
    config = OpenCodeProcessConfig(mcp_auth_token="")
    cmd = config.build_docker_run_command(effective_password="p")
    assert not any(p.startswith("FINBOARD_MCP_TOKEN=") for p in cmd)


def test_build_docker_stop_command() -> None:
    config = OpenCodeProcessConfig(container_name="my-oc")
    cmd = config.build_docker_stop_command()
    assert cmd == ["docker", "rm", "-f", "my-oc"]


def test_base_url() -> None:
    config = OpenCodeProcessConfig(port=4097, hostname="127.0.0.1")
    assert config.base_url == "http://127.0.0.1:4097"


def test_resolved_password_placeholder_when_empty() -> None:
    config = OpenCodeProcessConfig(password="")
    # 配置层只暴露占位符;真实密码由 ProcessManager.__init__ 生成。
    assert config.resolved_password


# ---------------------------------------------------------------------------
# OpenCodeProcessConfig — 环境变量白名单
# ---------------------------------------------------------------------------


def test_build_environment_strict_whitelist_no_leak() -> None:
    """严格白名单:FinBoard 的 DB 密码 / broker 凭证 / API Key 不进 docker CLI 子进程。"""
    parent_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "FINBOARD_DB_URL": "postgresql://secret:supersecret@host/db",
        "FINBOARD_QMT_PASSWORD": "broker-secret",
        "FINBOARD_LLM_API_KEY": "sk-super-secret",
        "OPENAI_API_KEY": "sk-leak",
    }
    config = OpenCodeProcessConfig()
    env = config.build_environment(parent_env=parent_env)
    # 继承白名单。
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"
    # 敏感字段绝不泄露。
    assert "FINBOARD_DB_URL" not in env
    assert "FINBOARD_QMT_PASSWORD" not in env
    assert "FINBOARD_LLM_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_environment_includes_docker_cli_extras() -> None:
    """docker CLI 在 Windows 上需要 COMSPEC / ProgramFiles(Docker Desktop 路径)。"""
    config = OpenCodeProcessConfig()
    env = config.build_environment(
        parent_env={
            "PATH": "/usr/bin",
            "COMSPEC": r"C:\Windows\system32\cmd.exe",
            "ProgramFiles": r"C:\Program Files",
        }
    )
    assert env["COMSPEC"] == r"C:\Windows\system32\cmd.exe"
    assert env["ProgramFiles"] == r"C:\Program Files"


# ---------------------------------------------------------------------------
# OpenCodeProcessManager — 健康探测 / 状态(非托管模式,无真实容器)
# ---------------------------------------------------------------------------


@pytest.fixture
def unmanaged_manager() -> OpenCodeProcessManager:
    """非托管管理器(不启动容器,用于测试健康探测 / 状态)。"""
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
    assert status.container_id is None
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
    # 非托管模式 start 不启动容器。
    await unmanaged_manager.start()
    assert unmanaged_manager.container_id is None


# ---------------------------------------------------------------------------
# OpenCodeProcessManager — 托管模式 docker CLI 不可用
# ---------------------------------------------------------------------------


async def test_managed_start_requires_docker(work_tmp, monkeypatch) -> None:
    """托管模式 docker CLI 不存在时报 OpenCodeProcessError(降级,不炸 lifespan)。"""
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    # docker CLI 解析失败。
    monkeypatch.setattr(
        "finboard_opencode.process_manager._resolve_docker_binary", lambda: None
    )
    with pytest.raises(OpenCodeProcessError, match="docker CLI not found"):
        await manager.start()


async def test_managed_double_start_raises(work_tmp, monkeypatch) -> None:
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    # 模拟容器已在运行(is_running 返回 True)。
    manager._container_id = "abc123def456"
    monkeypatch.setattr(
        "finboard_opencode.process_manager._docker_container_running", lambda name: True
    )
    with pytest.raises(OpenCodeProcessError, match="already running"):
        await manager.start()


async def test_remove_stale_container_calls_docker_rm(
    work_tmp, monkeypatch
) -> None:
    """``_remove_stale_container`` 用 ``docker rm -f`` 清理同名容器(幂等)。"""
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
        container_name="test-stale-oc",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    monkeypatch.setattr(
        "finboard_opencode.process_manager._resolve_docker_binary",
        lambda: "/fake/docker",
    )
    # 拦截整个 subprocess 执行,直接返回成功(stdout = 容器名,表示已移除)。
    captured_cmds: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"test-stale-oc\n", b"")

    async def _fake_create_exec(*args: str, **kwargs: object) -> _FakeProc:
        captured_cmds.append(list(args))
        return _FakeProc()

    # 非托管 _executor 线程:直接在当前循环跑 coro_factory。
    async def _fake_executor_run(coro_factory):
        return await coro_factory()

    monkeypatch.setattr(manager._executor, "run", _fake_executor_run)
    # _remove_stale_container 内部用 create_subprocess_exec;patch asyncio 模块级。
    monkeypatch.setattr(
        "finboard_opencode.process_manager.asyncio.create_subprocess_exec",
        _fake_create_exec,
    )
    await manager._remove_stale_container()
    assert len(captured_cmds) == 1
    assert captured_cmds[0][0] == "/fake/docker"
    assert "rm" in captured_cmds[0]
    assert "-f" in captured_cmds[0]
    assert "test-stale-oc" in captured_cmds[0]


async def test_remove_stale_container_noop_without_docker(
    work_tmp, monkeypatch
) -> None:
    """docker CLI 不可用时 ``_remove_stale_container`` 静默跳过。"""
    config = OpenCodeProcessConfig(
        workdir=str(work_tmp),
        log_path=str(work_tmp / "log" / "oc.log"),
        password="p",
    )
    manager = OpenCodeProcessManager(config, manage_process=True)
    monkeypatch.setattr(
        "finboard_opencode.process_manager._resolve_docker_binary", lambda: None
    )
    # 不应抛异常。
    await manager._remove_stale_container()


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


def test_resolve_docker_binary_returns_str_or_none() -> None:
    # docker 可能装了也可能没装;只要不抛异常即可。
    result = _resolve_docker_binary()
    assert result is None or isinstance(result, str)


def test_resolve_docker_binary_falls_back_to_windows_install_path() -> None:
    """``shutil.which`` 失败时,回退探测 Windows Docker Desktop 标准安装路径。

    场景:FinBoard 从 IDE / 服务 / 非交互 shell 启动,Docker 的 bin 不在 PATH。
    构造一个假的 docker.exe 在 ProgramFiles 候选路径下,验证回退命中。
    """
    if sys.platform != "win32":
        pytest.skip("Windows 安装路径回退仅在 win32 生效")

    with tempfile.TemporaryDirectory() as tmp:
        fake_program_files = Path(tmp) / "ProgramFiles"
        docker_bin = fake_program_files / "Docker" / "Docker" / "resources" / "bin"
        docker_bin.mkdir(parents=True)
        fake_docker = docker_bin / "docker.exe"
        fake_docker.write_bytes(b"fake")

        with patch.dict(
            os.environ,
            {"ProgramFiles": str(fake_program_files), "ProgramData": str(tmp)},
        ), patch("finboard_opencode.process_manager.shutil.which", lambda _: None):
            result = _resolve_docker_binary()
        assert result == str(fake_docker)


def test_resolve_docker_binary_returns_none_when_nowhere() -> None:
    """``which`` 失败且 Windows 候选路径都不存在时返回 None。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch(
            "finboard_opencode.process_manager.shutil.which", lambda _: None
        ), patch.dict(
            os.environ,
            {
                "ProgramFiles": str(Path(tmp) / "nope"),
                "ProgramFiles(x86)": str(Path(tmp) / "nope86"),
                "LOCALAPPDATA": str(Path(tmp) / "nope-local"),
                "ProgramData": str(Path(tmp) / "nope-data"),
            },
        ):
            assert _resolve_docker_binary() is None


def test_windows_docker_candidates_dedup() -> None:
    """候选路径去重(不同 env var 可能指向同一目录)。"""
    if sys.platform != "win32":
        pytest.skip("Windows 候选路径仅在 win32 展开")
    same = "C:\\Same"
    with patch.dict(
        os.environ,
        {"ProgramFiles": same, "ProgramFiles(x86)": same, "LOCALAPPDATA": ""},
        clear=False,
    ):
        candidates = _windows_docker_binary_candidates()
    # ProgramFiles 与 ProgramFiles(x86) 展开相同路径,去重后只保留一个。
    assert len({p for p in candidates if p.startswith(same)}) == 1


def test_docker_container_running_returns_bool() -> None:
    # 对不存在的容器名应返回 False(保守判定)。
    with patch(
        "finboard_opencode.process_manager._resolve_docker_binary", lambda: None
    ):
        assert _docker_container_running("nonexistent-xyz-12345") is False
