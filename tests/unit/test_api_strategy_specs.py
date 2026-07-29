"""无代码策略注册/模板 API 与安全输入边界。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import strategy_specs_router
from finboard_backtest.strategy_spec import build_strategy_template


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(strategy_specs_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def test_registry_and_template_are_no_code_contracts(client: TestClient) -> None:
    registry = client.get("/api/research/strategy-specs/registry")
    assert registry.status_code == 200
    body = registry.json()
    assert body["accepts_python"] is False
    assert body["publication_starts_run"] is False
    assert all(item["can_execute_on_publish"] is False for item in body["strategies"])
    assert all(item["executable_expression"] is False for item in body["operators"])

    template = client.get(
        "/api/research/strategy-specs/templates/etf_rotation",
        params={
            "strategy_id": "api_etf_rotation",
            "dataset_release_ids": "release-v1",
        },
    )
    assert template.status_code == 200
    assert template.json()["strategy_kind"] == "etf_rotation"
    assert "python" not in template.text.lower()


@pytest.mark.parametrize(
    "injection",
    [
        {"python_code": "print('trade')"},
        {"module_path": "strategies.alpha"},
        {"expression": "eval('1+1')"},
    ],
)
def test_api_rejects_executable_and_overprivileged_fields(
    client: TestClient,
    injection: dict[str, str],
) -> None:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="secure_factor",
        dataset_release_ids=("release-v1",),
    ).canonical_payload()
    spec.update(injection)
    response = client.post(
        "/api/research/strategy-specs/drafts",
        json={"spec": spec, "expected_version": None},
    )
    assert response.status_code == 422


def test_api_rejects_template_injection_in_nested_text(client: TestClient) -> None:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="secure_nested",
        dataset_release_ids=("release-v1",),
    ).canonical_payload()
    spec["signal_rules"]["rules"][0]["rationale"] = "{{ import os }}"
    response = client.post(
        "/api/research/strategy-specs/drafts",
        json={"spec": spec},
    )
    assert response.status_code == 422
