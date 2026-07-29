"""ResearchRun API security and no-execution boundary."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import research_runs_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(research_runs_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def test_api_exposes_queue_history_cancel_replay_but_no_execute(
    client: TestClient,
) -> None:
    paths = cast(FastAPI, client.app).openapi()["paths"]
    assert "/api/research/runs" in paths
    assert "/api/research/runs/{run_id}/cancel" in paths
    assert "/api/research/runs/{run_id}/replay" in paths
    assert all(
        not path.endswith("/execute") and not path.endswith("/run")
        for path in paths
    )


def test_api_rejects_llm_actor_before_repository_access(client: TestClient) -> None:
    response = client.post(
        "/api/research/runs",
        json={
            "idempotency_key": "llm-must-not-trigger",
            "strategy_id": "secure_strategy",
            "strategy_version": 1,
            "dataset_release_ids": ["release-v1"],
            "code_version": "abcdef0",
            "initial_capital": "100000",
            "requested_by": "assistant",
            "actor_type": "llm",
        },
    )
    assert response.status_code == 422


def test_api_rejects_python_strategy_payload(client: TestClient) -> None:
    response = client.post(
        "/api/research/runs",
        json={
            "idempotency_key": "python-not-supported",
            "strategy_id": "secure_strategy",
            "strategy_version": 1,
            "dataset_release_ids": ["release-v1"],
            "code_version": "abcdef0",
            "initial_capital": "100000",
            "requested_by": "human",
            "actor_type": "human",
            "python_code": "print('trade')",
        },
    )
    assert response.status_code == 422
