"""OpenCode Web 网关路由单元测试(issue #118)。

用 FastAPI TestClient + dependency_overrides 注入桩对象,验证:
* ``/status`` / ``/health`` / ``/access`` 在网关关闭时返回 503;
* ``/status`` 返回脱敏状态快照(不含密码);
* ``/health`` 代理探测;
* ``/access`` 成功签发凭证 + 授权失败 403 + 会话不存在 404。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import (
    get_conversation_service,
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
from finboard_opencode.schemas import ConversationRecord, ConversationStatus

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
            pid=None,
            base_url=self.base_url,
            healthy=self._stub_healthy,
            version=self._version,
            started_at=datetime.now(UTC),
        )

    async def health(self) -> dict[str, Any] | None:
        if not self._stub_healthy:
            return None
        return {"healthy": True, "version": self._version}


class StubConversationService:
    """桩:返回预设的 conversation record。"""

    def __init__(self, record: ConversationRecord | None) -> None:
        self._record = record

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        return self._record


def _make_record(
    *, status: ConversationStatus = ConversationStatus.ACTIVE
) -> ConversationRecord:
    now = datetime.now(UTC)
    return ConversationRecord(
        conversation_id="CONV-20260101-deadbeefdeadbeef",
        opencode_session_id="sess-xyz",
        agent_run_id=None,
        title="t",
        status=status,
        agent_name="finboard-researcher",
        model_ref=None,
        last_event_seq=0,
        created_at=now,
        updated_at=now,
    )


def _build_app(
    *,
    manager: OpenCodeProcessManager | None,
    issuer: AccessCredentialIssuer | None,
    service: Any,
) -> FastAPI:
    app = FastAPI()
    app.include_router(opencode_gateway_router)

    app.dependency_overrides[get_opencode_process_manager] = lambda: manager
    app.dependency_overrides[get_opencode_access_issuer] = lambda: issuer
    app.dependency_overrides[get_conversation_service] = lambda: service
    return app


# ---------------------------------------------------------------------------
# 503 回退(网关关闭)
# ---------------------------------------------------------------------------


def test_status_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None, service=StubConversationService(None))
    with TestClient(app) as client:
        resp = client.get("/api/opencode/status")
    assert resp.status_code == 503


def test_health_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None, service=StubConversationService(None))
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 503


def test_access_returns_503_when_disabled() -> None:
    app = _build_app(manager=None, issuer=None, service=StubConversationService(None))
    with TestClient(app) as client:
        resp = client.post("/api/opencode/access", json={"conversation_id": "CONV-x"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# status / health(网关启用)
# ---------------------------------------------------------------------------


def test_status_returns_snapshot_without_password() -> None:
    manager = StubProcessManager(healthy=True)
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(
        manager=manager, issuer=issuer, service=StubConversationService(None)
    )
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
    app = _build_app(
        manager=manager, issuer=None, service=StubConversationService(None)
    )
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["version"] == "1.18.15"


def test_health_reports_unhealthy_when_down() -> None:
    manager = StubProcessManager(healthy=False)
    app = _build_app(
        manager=manager, issuer=None, service=StubConversationService(None)
    )
    with TestClient(app) as client:
        resp = client.get("/api/opencode/health")
    assert resp.status_code == 200
    assert resp.json()["healthy"] is False


# ---------------------------------------------------------------------------
# access(凭证签发)
# ---------------------------------------------------------------------------


def test_access_issues_credential_for_active_conversation() -> None:
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    record = _make_record()
    app = _build_app(
        manager=manager, issuer=issuer, service=StubConversationService(record)
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/opencode/access",
            json={"conversation_id": record.conversation_id},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == record.conversation_id
    assert body["opencode_session_id"] == "sess-xyz"
    assert body["web_url"] == "http://127.0.0.1:4097"
    assert body["username"] == "opencode"
    assert body["password"] == "stub-secret"
    assert body["agent_name"] == "finboard-researcher"


def test_access_rejects_non_active_conversation() -> None:
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    record = _make_record(status=ConversationStatus.COMPLETED)
    app = _build_app(
        manager=manager, issuer=issuer, service=StubConversationService(record)
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/opencode/access",
            json={"conversation_id": record.conversation_id},
        )
    assert resp.status_code == 403


def test_access_rejects_missing_conversation() -> None:
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(
        manager=manager, issuer=issuer, service=StubConversationService(None)
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/opencode/access",
            json={"conversation_id": "CONV-missing"},
        )
    # service.get_conversation 返回 None → issuer 抛 AccessNotAuthorizedError → 403
    assert resp.status_code == 403


def test_access_validates_request_body() -> None:
    manager = StubProcessManager()
    issuer = AccessCredentialIssuer(manager)
    app = _build_app(
        manager=manager, issuer=issuer, service=StubConversationService(None)
    )
    with TestClient(app) as client:
        resp = client.post("/api/opencode/access", json={})
    assert resp.status_code == 422
