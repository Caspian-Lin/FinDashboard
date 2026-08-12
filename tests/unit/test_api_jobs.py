"""``/api/jobs`` 路由契约与白名单边界(issue #117 / #142)。

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

