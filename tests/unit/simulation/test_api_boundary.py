"""模拟 API 不提供直接订单或实盘能力。"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import simulation_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(simulation_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def test_openapi_has_target_decision_but_no_direct_order_creation() -> None:
    client = _client()
    paths = cast(FastAPI, client.app).openapi()["paths"]
    decision_path = "/api/simulation/sessions/{session_id}/decisions"
    assert set(paths[decision_path]) == {"get", "post"}
    order_path = "/api/simulation/sessions/{session_id}/orders"
    assert set(paths[order_path]) == {"get"}
    assert all("broker" not in path and "qmt" not in path for path in paths)


def test_schema_rejects_python_and_live_account_ids_before_db_access() -> None:
    client = _client()
    python_response = client.post(
        "/api/simulation/accounts",
        json={
            "name": "safe",
            "initial_cash": "100000",
            "actor": "user",
            "python_code": "print('order')",
        },
    )
    assert python_response.status_code == 422

    live_id_response = client.post(
        "/api/simulation/sessions",
        json={
            "simulation_account_id": "real-account",
            "strategy_id": "strategy",
            "strategy_version": 1,
            "validation_run_id": "RR-test",
            "data_release_id": "release",
            "source_mode": "historical_replay",
            "actor": "user",
        },
    )
    assert live_id_response.status_code == 422
