"""ResearchRun API security and no-execution boundary."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import research_runs_router

#: dev 基线(#513 之前)的入队 / 重放请求体字段集合 —— OpenAPI 快照据此锁定,
#: 字段增删必须显式同步(客户端按组件名 + 字段生成 stub)。
QUEUE_BODY_FIELDS = {
    "idempotency_key",
    "strategy_id",
    "strategy_version",
    "dataset_release_ids",
    "factor_snapshot_ids",
    "factor_series_ids",
    "parameters",
    "validation_config",
    "portfolio_config",
    "risk_config",
    "execution_config",
    "fee_config",
    "benchmark_config",
    "code_version",
    "initial_capital",
    "requested_by",
    "actor_type",
    "data_scope",
    "data_scope_resolution_id",
}

REPLAY_BODY_FIELDS = {"idempotency_key", "requested_by", "actor_type"}


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


def _request_schema_ref(schema: dict[str, object], path: str) -> object:
    paths = cast("dict[str, dict[str, object]]", schema["paths"])
    operation = cast("dict[str, object]", paths[path]["post"])
    request_body = cast("dict[str, object]", operation["requestBody"])
    content = cast("dict[str, dict[str, object]]", request_body["content"])
    return content["application/json"]["schema"]


def test_openapi_request_component_names_and_fields_are_stable(
    client: TestClient,
) -> None:
    """#513 回归:入队 / 重放请求体的 OpenAPI 组件名与字段集合保持 dev 基线。

    issue #513 把 DTO 下沉到 ``finboard_app``;若路由直接用基类,组件名会从
    ``ResearchRunQueueIn`` 变成 ``ResearchRunSubmitInput``,按 dev 生成 stub 的
    客户端会断。这里同时锁定组件名与字段集合(新增字段必须显式同步)。
    """

    schema = cast("dict[str, object]", cast(FastAPI, client.app).openapi())
    components = cast("dict[str, dict[str, object]]", schema["components"])
    schemas = cast("dict[str, dict[str, object]]", components["schemas"])

    queue_ref = _request_schema_ref(schema, "/api/research/runs")
    assert queue_ref == {"$ref": "#/components/schemas/ResearchRunQueueIn"}
    queue_properties = cast("dict[str, object]", schemas["ResearchRunQueueIn"]["properties"])
    assert set(queue_properties) == QUEUE_BODY_FIELDS

    replay_ref = _request_schema_ref(schema, "/api/research/runs/{run_id}/replay")
    assert replay_ref == {"$ref": "#/components/schemas/ResearchRunReplayIn"}
    replay_properties = cast(
        "dict[str, object]", schemas["ResearchRunReplayIn"]["properties"]
    )
    assert set(replay_properties) == REPLAY_BODY_FIELDS


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
