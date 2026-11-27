"""Data scheduling preferences and sanitized source diagnostics (#525)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.routes import data


@pytest.fixture
def config_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    path = tmp_path / "data_config.json"
    monkeypatch.setattr(data, "_CONFIG_FILE", str(path))
    app = FastAPI()
    app.include_router(data.router)
    app.state.settings = SimpleNamespace(
        data_provider="tushare",
        tushare_token="test-secret",
        research_sandbox_enabled=False,
    )
    return TestClient(app), path


def test_legacy_defaults_conflict_and_refresh(config_api: tuple[TestClient, Path]) -> None:
    client, path = config_api
    path.write_text(json.dumps({"sync_enabled": False, "legacy_field": 7}), encoding="utf-8")
    original = client.get("/api/data/config").json()
    assert original["download_time"] == "15:45"
    assert original["sync_enabled"] is False

    first = client.put("/api/data/config", json={"sync_time": "12:00", "expected_revision": original["revision"]})
    assert first.status_code == 200
    assert first.json()["revision"] != original["revision"]
    conflict = client.put("/api/data/config", json={"sync_time": "13:00", "expected_revision": original["revision"]})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "schedule_conflict"
    assert client.get("/api/data/config").json()["sync_time"] == "12:00"
    assert json.loads(path.read_text(encoding="utf-8"))["legacy_field"] == 7


def test_source_status_does_not_return_credentials(config_api: tuple[TestClient, Path]) -> None:
    client, _ = config_api
    response = client.get("/api/data/source-status")
    assert response.json() == {"provider": "tushare", "tushare_configured": True, "sandbox_enabled": False}


def test_invalid_configuration_has_actionable_code(config_api: tuple[TestClient, Path]) -> None:
    client, path = config_api
    path.write_text("invalid JSON", encoding="utf-8")
    response = client.get("/api/data/config")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "schedule_invalid"
