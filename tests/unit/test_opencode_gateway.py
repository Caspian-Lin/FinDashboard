"""OpenCode Web 网关路由单元测试(issue #118 / 重构 #121 / #157)。

用 FastAPI TestClient + dependency_overrides 注入桩对象,验证:
* ``/status`` / ``/health`` / ``/access`` 在网关关闭时返回 503;
* ``/status`` 返回状态快照 + 内嵌 FinBoard MCP 状态块(#157);
* ``/health`` 代理探测;
* ``/access`` 签发明文 ``web_url``(#157 后无凭证字段;#121 后不绑定 conversation)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import (
    get_opencode_access_issuer,
    get_opencode_process_manager,
)
from finboard_api.routes.opencode_gateway import router as opencode_gateway_router
from finboard_opencode import (
    AccessIssuer,
    OpenCodeProcessConfig,
    OpenCodeProcessManager,
    ProcessStatus,
)

# ---------------------------------------------------------------------------
# 桩对象
# ---------------------------------------------------------------------------


class StubProcessManager(OpenCodeProcessManager):
    """避免真实子进程:覆写 status / health。"""

    def __init__(self, *, healthy: bool = True, version: str = "1.18.15") -> None:
        config = OpenCodeProcessConfig(port=4097, hostname="127.0.0.1")
        super().__init__(config, manage_process=False)
        self._stub_healthy = healthy
        self._version = version

    async def status(self) -> ProcessStatus:
        return ProcessStatus(
            running=True,
            managed=False,
            container_id=None,
            base_url=self.base_url,
            healthy=self._stub_healthy,
            version=self._version,
            started_at=datetime.now(UTC),
        )

    async def health(self) -> dict[str, Any] | None:
        if not self._stub_healthy:
            return None
        return {"healthy": True, "version": self._version}


def _make_settings(**overrides: Any) -> MagicMock:
    defaults = {
        "opencode_embed_mcp": True,
        "mcp_port": 8765,
        "mcp_auth_token": "secret-token",
        "opencode_mcp_remote_url": "http://host.docker.internal:8765/mcp",
    }
    defaults.update(overrides)
    settings = MagicMock()
    for key, value in defaults.items():
        setattr(settings, key, value)
    return settings


def _build_app(
    *,
    manager: OpenCodeProcessManager | None,
    issuer: AccessIssuer | None,
    mcp_running: bool = False,
    settings: MagicMock | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(opencode_gateway_router)
    app.state.settings = settings or _make_settings()
    if mcp_running:
        app.state.opencode_mcp_server = MagicMock()
        app.state.opencode_mcp_host = "0.0.0.0"
    else:
        app.state.opencode_mcp_server = None
        app.state.opencode_mcp_host = None

    app.dependency_overrides[get_opencode_process_manager] = lambda: manager
    app.dependency_overrides[get_opencode_access_issuer] = lambda: issuer
    return app


# ---------------------------------------------------------------------------
# 503 回退(网关关闭)
# ---------------------------------------------------------------------------


def test_status_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/status")
    assert resp.status_code == 503


def test_health_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 503


def test_access_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None)
    with TestClient(app) as client:
        resp = client.post("/api/opencode/access")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# status / health(网关启用)
# ---------------------------------------------------------------------------


def test_status_returns_snapshot_with_mcp_block() -> None:
    manager = StubProcessManager(healthy=True)
    issuer = AccessIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer, mcp_running=True)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["managed"] is False
    assert body["base_url"] == "http://127.0.0.1:4097"
    assert body["healthy"] is True
    assert body["version"] == "1.18.15"
    # #157:MCP 状态块(内嵌运行 + 远端 URL + 鉴权标记)。
    mcp = body["mcp"]
    assert mcp["embedded_configured"] is True
    assert mcp["embedded_running"] is True
    assert mcp["host"] == "0.0.0.0"
    assert mcp["port"] == 8765
    assert mcp["remote_url"] == "http://host.docker.internal:8765/mcp"
    assert mcp["auth"] is True


def test_status_mcp_block_reports_not_running_when_lifespan_skipped() -> None:
    """内嵌 MCP 未运行(如缺 token 被 lifespan 跳过)时如实报告。"""
    manager = StubProcessManager(healthy=True)
    app = _build_app(manager=manager, issuer=None, mcp_running=False)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/status")
    assert resp.status_code == 200
    mcp = resp.json()["mcp"]
    assert mcp["embedded_configured"] is True
    assert mcp["embedded_running"] is False
    assert mcp["host"] is None


def test_health_probes_upstream() -> None:
    manager = StubProcessManager(healthy=True)
    app = _build_app(manager=manager, issuer=None)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["version"] == "1.18.15"


def test_health_reports_unhealthy_when_down() -> None:
    manager = StubProcessManager(healthy=False)
    app = _build_app(manager=manager, issuer=None)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 200
    assert resp.json()["healthy"] is False


# ---------------------------------------------------------------------------
# access(#157 明文 web_url,无凭证 —— #121 后不绑定 conversation)
# ---------------------------------------------------------------------------


def test_access_issues_plain_url_when_gateway_enabled() -> None:
    manager = StubProcessManager()
    issuer = AccessIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer)
    with TestClient(app) as client:
        resp = client.post("/api/opencode/access")
    assert resp.status_code == 200
    body = resp.json()
    assert body["web_url"] == "http://127.0.0.1:4097"
    assert body["agent_name"] == "finboard-researcher"
    # #157 移除 basic auth 后不再有 username/password;
    # #121 重构后不再有 conversation 绑定字段。
    assert "username" not in body
    assert "password" not in body
    assert "conversation_id" not in body
    assert "opencode_session_id" not in body


def test_access_does_not_require_request_body() -> None:
    """#121 重构后 /access 不再需要 conversation_id,空 body 即可。"""
    manager = StubProcessManager()
    issuer = AccessIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer)
    with TestClient(app) as client:
        # 不传 body 也能签发
        resp = client.post("/api/opencode/access")
    assert resp.status_code == 200
