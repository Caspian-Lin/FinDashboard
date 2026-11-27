from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api import research_distribution
from finboard_api.routes import research_audit
from finboard_mcp.identity import McpClientIdentity


def test_research_distribution_checks_audit_read_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINBOARD_RESEARCH_WEB_ROOT", str(tmp_path))
    requested: list[AsyncSession] = []

    async def resolve(token: str) -> McpClientIdentity | None:
        if token == "audit-test":
            return McpClientIdentity("MC-audit", "test-audit", frozenset({"audit.read"}))
        if token == "reader-test":
            return McpClientIdentity("MC-reader", "test-reader", frozenset({"research.read"}))
        return None

    class AuditRepository:
        def __init__(self, session: AsyncSession) -> None:
            requested.append(session)

        async def coverage(self) -> dict[str, object]:
            return {
                "from": None, "to": datetime(2026, 11, 27, tzinfo=UTC), "total": 7,
                "server_observed_from": None, "client_declared_from": None,
            }

    monkeypatch.setattr(research_distribution, "DatabaseIdentityResolver", lambda _sessions: resolve)
    monkeypatch.setattr(research_audit, "ResearchAuditRepository", AuditRepository)
    client = TestClient(research_distribution.create_app(), base_url="http://127.0.0.1")
    path = "/api/research/audit/coverage"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer reader-test"}).status_code == 403
    assert not requested
    response = client.get(path, headers={"Authorization": "Bearer audit-test"})
    assert response.status_code == 200
    assert response.json()["total"] == 7
    assert len(requested) == 1
