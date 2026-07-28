"""portfolio API 端点测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finboard_api.app import create_app


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestAllocateEndpoint:
    def test_equal_weight(self, client: TestClient) -> None:
        resp = client.post("/api/portfolio/allocate", json={
            "signals": [
                {"symbol": "A", "score": 1.0},
                {"symbol": "B", "score": 1.0},
            ],
            "method": "equal_weight",
            "as_of": "2024-06-28",
            "strategy_id": "test",
            "min_cash_buffer": 0.0,
            "max_weight_per_asset": 0.80,
            "max_weight_per_sleeve": 0.90,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["n_assets"] == 2
        assert len(data["weights"]) == 2

    def test_empty_signals(self, client: TestClient) -> None:
        resp = client.post("/api/portfolio/allocate", json={
            "signals": [],
            "method": "equal_weight",
            "as_of": "2024-06-28",
        })
        assert resp.status_code == 400

    def test_erc_with_returns(self, client: TestClient) -> None:
        import numpy as np
        rng = np.random.default_rng(42)
        resp = client.post("/api/portfolio/allocate", json={
            "signals": [
                {"symbol": "A", "score": 1.0},
                {"symbol": "B", "score": 1.0},
                {"symbol": "C", "score": 1.0},
            ],
            "method": "erc",
            "as_of": "2024-06-28",
            "min_cash_buffer": 0.0,
            "max_weight_per_asset": 0.80,
            "max_weight_per_sleeve": 0.90,
            "returns_by_ticker": {
                "A": (rng.standard_normal(100) * 0.01).tolist(),
                "B": (rng.standard_normal(100) * 0.02).tolist(),
                "C": (rng.standard_normal(100) * 0.03).tolist(),
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["covariance_shrinkage"] is not None
        assert 0 <= data["covariance_shrinkage"] <= 1


class TestSizingEndpoint:
    def test_basic(self, client: TestClient) -> None:
        resp = client.post("/api/portfolio/sizing", json={
            "weights": {"A": 0.50, "B": 0.40},
            "as_of": "2024-06-28",
            "strategy_id": "test",
            "capital": 100000.0,
            "lot_info": [
                {"code": "A", "lot_size": 100, "multiplier": 1},
                {"code": "B", "lot_size": 100, "multiplier": 1},
            ],
            "prices": {"A": 10.0, "B": 20.0},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash_after"] >= 0
        assert data["n_active_trades"] >= 1

    def test_invalid_capital(self, client: TestClient) -> None:
        resp = client.post("/api/portfolio/sizing", json={
            "weights": {"A": 0.50},
            "as_of": "2024-06-28",
            "capital": -100,
            "lot_info": [{"code": "A", "lot_size": 100}],
            "prices": {"A": 10.0},
        })
        assert resp.status_code == 422


class TestAttributionEndpoint:
    def test_basic(self, client: TestClient) -> None:
        import numpy as np
        rng = np.random.default_rng(42)
        resp = client.post("/api/portfolio/attribution", json={
            "weights_history": [{"A": 0.50, "B": 0.50}],
            "returns_by_ticker": {
                "A": (rng.standard_normal(100) * 0.01).tolist(),
                "B": (rng.standard_normal(100) * 0.02).tolist(),
            },
            "sleeve_map": {"A": "equity", "B": "bond"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["by_asset"]) == 2
        assert len(data["by_sleeve"]) == 2

    def test_empty(self, client: TestClient) -> None:
        resp = client.post("/api/portfolio/attribution", json={
            "weights_history": [],
            "returns_by_ticker": {},
        })
        assert resp.status_code == 400
