"""``/api/jobs`` 路由契约与白名单边界(issue #117 / #142;#221 归档)。

只验证路由形状、kind 白名单、入参校验契约;
真正的 PG 状态机 / SKIP LOCKED / worker 端到端在
``tests/integration/test_background_job_persistence.py`` 覆盖。
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import jobs_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(jobs_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def test_routes_registered(client: TestClient) -> None:
    paths = cast(FastAPI, client.app).openapi()["paths"]
    assert "/api/jobs" in paths
    assert "/api/jobs/{job_id}" in paths
    assert "/api/jobs/{job_id}/cancel" in paths
    # issue #221:归档 / 取消归档 / 批量归档端点注册
    assert "/api/jobs/{job_id}/archive" in paths
    assert "/api/jobs/{job_id}/unarchive" in paths
    assert "/api/jobs/archive" in paths
    # 不暴露任何 execute / run 端点(执行由独立 worker 进程承接)
    assert all(
        not p.endswith("/execute") and not p.endswith("/run") for p in paths
    )


def test_rejects_unknown_kind(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "kind": "definitely_not_registered",
            "idempotency_key": "idem-unknown-kind",
            "requested_by": "tester",
        },
    )
    assert response.status_code == 422
    assert "未开放" in response.json()["detail"]


def test_rejects_short_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "kind": "echo",
            "idempotency_key": "short",  # min_length=8
            "requested_by": "tester",
        },
    )
    assert response.status_code == 422


def test_priority_bounds_enforced(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "kind": "echo",
            "idempotency_key": "idem-priority-ok",
            "priority": 100_000,  # ge=-1000, le=1000
            "requested_by": "tester",
        },
    )
    assert response.status_code == 422


def test_extra_fields_forbidden(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "kind": "echo",
            "idempotency_key": "idem-extra-field",
            "requested_by": "tester",
            "secret_payload": "should-be-rejected",  # extra="forbid"
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- issue #221


def test_list_rejects_unknown_archived_filter(client: TestClient) -> None:
    response = client.get("/api/jobs", params={"archived": "sometimes"})
    assert response.status_code == 422
    assert "归档过滤值" in response.json()["detail"]


def test_list_accepts_valid_archived_filters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exclude/only/all 均通过参数校验(handler 内走 mock repo,不触库)。"""

    from finboard_persistence.background_job_repo import BackgroundJobRepository

    monkeypatch.setattr(
        BackgroundJobRepository, "list_recent", AsyncMock(return_value=[])
    )
    for value in ("exclude", "only", "all"):
        response = client.get("/api/jobs", params={"archived": value, "limit": 1})
        assert response.status_code == 200, value
        assert response.json() == []


def test_bulk_archive_rejects_non_terminal_statuses(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/archive",
        json={"statuses": ["running", "queued"]},
    )
    assert response.status_code == 422
    assert "终态" in response.json()["detail"]


def test_bulk_archive_limit_bounds(client: TestClient) -> None:
    response = client.post("/api/jobs/archive", json={"limit": 9999})
    assert response.status_code == 422


def test_bulk_archive_extra_fields_forbidden(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/archive",
        json={"statuses": ["succeeded"], "dry_run": True},
    )
    assert response.status_code == 422

