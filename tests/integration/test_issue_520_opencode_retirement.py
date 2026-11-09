"""#520 退役内置 OpenCode 托管:无配置 / 无 Docker 的启动与服务演练。

* 升级演练:配置里完全没有 ``opencode_*`` 字段、机器上没有 Docker 时,
  ``finboard_api`` 仍能完成 lifespan 并服务研究端点;
* 旧配置演练:环境里残留 ``FINBOARD_OPENCODE_*`` 时启动不失败,但会打一行
  弃用警告列出仍然存在的键(便于清理);
* ``/api/opencode/*`` 网关已随 #520 删除(404)。
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient
from structlog.testing import CapturingLogger

import finboard_api.app as app_module
from finboard_app.config import Settings, retired_opencode_env_keys
from finboard_shared.types import BrokerKind
from tests.integration.conftest import TEST_DB_URL

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    # _env_file=None:不继承本机 .env(否则旧 FINBOARD_OPENCODE_* 会由本机
    # 环境带进来,掩盖"配置缺席"的前提)。显式字段优先于进程环境变量。
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        broker=BrokerKind.MOCK,
        account_id="test-account-api-520",
        db_url=TEST_DB_URL,
        risk_allow_market_order=True,
        mcp_enabled=False,
    )


async def _assert_research_surface(client: AsyncClient) -> None:
    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["kernel_ready"] is True

    runs = await client.get("/api/research/runs?limit=1")
    assert runs.status_code == 200

    topics = await client.get("/api/research/topics?limit=1")
    assert topics.status_code == 200

    gateway = await client.get("/api/opencode/status")
    assert gateway.status_code == 404, "网关已随 #520 删除,不应再注册"


@pytest.mark.usefixtures("_api_engine")
async def test_api_starts_without_opencode_settings_or_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置缺席 + 无法执行任何子进程(等价无 Docker)时 API 照常启动。"""

    async def _no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("API 启动/请求不得依赖 docker/opencode 子进程")

    def _no_sync_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("API 启动/请求不得依赖 docker/opencode 子进程")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)
    monkeypatch.setattr(subprocess, "run", _no_sync_spawn)

    from finboard_api.app import create_app

    settings = _settings()
    assert not [name for name in dir(settings) if name.startswith("opencode_")]

    app = create_app(settings)
    assert not hasattr(app.state, "opencode_runtime")
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await _assert_research_surface(client)


@pytest.mark.usefixtures("_api_engine")
async def test_legacy_opencode_env_warns_but_startup_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧键升级演练:警告列出键名,API 仍可启动并服务研究端点。"""
    monkeypatch.setenv("FINBOARD_OPENCODE_WEB_ENABLED", "true")
    monkeypatch.setenv("FINBOARD_OPENCODE_MANAGE_PROCESS", "true")
    monkeypatch.setenv("FINBOARD_OPENCODE_IMAGE", "ghcr.io/anomalyco/opencode:1.18.15")

    keys = retired_opencode_env_keys(env_file=None)
    assert set(keys) >= {
        "FINBOARD_OPENCODE_IMAGE",
        "FINBOARD_OPENCODE_MANAGE_PROCESS",
        "FINBOARD_OPENCODE_WEB_ENABLED",
    }

    captured = CapturingLogger()
    monkeypatch.setattr(app_module, "logger", captured)

    app = app_module.create_app(_settings())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await _assert_research_surface(client)

    warnings = [
        call
        for call in captured.calls
        if call.method_name == "warning"
        and call.args
        and call.args[0] == "api.opencode_settings_retired"
    ]
    assert warnings, "启动必须打印 api.opencode_settings_retired 弃用警告"
    assert warnings[0].kwargs["keys"] == keys
    assert warnings[0].kwargs["removed_in"] == "#520"
