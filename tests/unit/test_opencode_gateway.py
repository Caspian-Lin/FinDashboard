"""OpenCode Web 网关路由单元测试(issue #118 / 重构 #121)。

用 FastAPI TestClient + dependency_overrides 注入桩对象,验证:
* ``/status`` / ``/health`` / ``/access`` 在网关关闭时返回 503;
* ``/status`` 返回脱敏状态快照(不含密码);
* ``/health`` 代理探测;
* ``/access`` 成功签发凭证(#121 重构后不绑定 conversation)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import (
    get_opencode_access_issuer,
    get_opencode_process_manager,
)
from finboard_api.routes.opencode_gateway import router as opencode_gateway_router
from finboard_opencode import (
    AccessCredentialIssuer,
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
        config = OpenCodeProcessConfig(
            port=4097, hostname="127.0.0.1", username="opencode", password="stub-secret"
        )
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


def _build_app(
    *,
    manager: OpenCodeProcessManager | None,
    issuer: AccessCredentialIssuer | None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(opencode_gateway_router)

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


def test_status_returns_snapshot_without_password() -> None:
    manager = StubProcessManager(healthy=True)
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer)
    with TestClient(app) as client:
        resp = client.get("/api/opencode/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["managed"] is False
    assert body["base_url"] == "http://127.0.0.1:4097"
    assert body["healthy"] is True
    assert body["version"] == "1.18.15"
    assert "password" not in body  # 脱敏:status 不含密码


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
# access(凭证签发 —— #121 重构后不绑定 conversation)
# ---------------------------------------------------------------------------


def test_access_issues_credential_when_gateway_enabled() -> None:
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer)
    with TestClient(app) as client:
        resp = client.post("/api/opencode/access")
    assert resp.status_code == 200
    body = resp.json()
    assert body["web_url"] == "http://127.0.0.1:4097"
    assert body["username"] == "opencode"
    assert body["password"] == "stub-secret"
    assert body["agent_name"] == "finboard-researcher"
    # #121 重构后 AccessOut 不再有 conversation_id / opencode_session_id
    assert "conversation_id" not in body
    assert "opencode_session_id" not in body


def test_access_does_not_require_request_body() -> None:
    """#121 重构后 /access 不再需要 conversation_id,空 body 即可。"""
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(manager=manager, issuer=issuer)
    with TestClient(app) as client:
        # 不传 body 也能签发
        resp = client.post("/api/opencode/access")
    assert resp.status_code == 200
