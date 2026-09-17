"""策略参数预设 API 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import strategy_presets_router


class FakeStrategyPresetRepository:
    rows: ClassVar[dict[int, SimpleNamespace]] = {}
    next_id: ClassVar[int] = 1

    def __init__(self, session: object) -> None:
        self.session = session

    async def create(
        self,
        *,
        name: str,
        strategy: str,
        params: dict[str, Any],
        selection: dict[str, Any],
    ) -> SimpleNamespace:
        now = datetime.now(UTC)
        row = SimpleNamespace(
            id=self.next_id,
            name=name,
            strategy=strategy,
            params=params,
            selection=selection,
            created_at=now,
            updated_at=now,
        )
        self.rows[row.id] = row
        type(self).next_id += 1
        return row

    async def list_all(self) -> list[SimpleNamespace]:
        return list(reversed(self.rows.values()))

    async def get(self, preset_id: int) -> SimpleNamespace | None:
        return self.rows.get(preset_id)

    async def get_by_name(self, name: str) -> SimpleNamespace | None:
        return next((row for row in self.rows.values() if row.name == name), None)

    async def update(
        self,
        preset_id: int,
        *,
        name: str,
        strategy: str,
        params: dict[str, Any],
        selection: dict[str, Any],
    ) -> SimpleNamespace | None:
        row = self.rows.get(preset_id)
        if row is None:
            return None
        row.name = name
        row.strategy = strategy
        row.params = params
        row.selection = selection
        row.updated_at = datetime.now(UTC)
        return row

    async def delete(self, preset_id: int) -> bool:
        return self.rows.pop(preset_id, None) is not None


@pytest.fixture
def client() -> TestClient:
    FakeStrategyPresetRepository.rows = {}
    FakeStrategyPresetRepository.next_id = 1
    app = FastAPI()
    app.include_router(strategy_presets_router)
    session = AsyncMock()
    app.dependency_overrides[get_db_session] = lambda: session
    return TestClient(app)


def test_strategy_preset_crud_and_schema_validation(client: TestClient) -> None:
    with patch(
        "finboard_api.routes.strategy_presets.StrategyPresetRepository",
        FakeStrategyPresetRepository,
    ):
        created = client.post(
            "/api/strategy-presets",
            json={
                "name": " 中期趋势 ",
                "strategy": "ma_cross",
                "params": {"short_window": "10", "long_window": "30"},
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "中期趋势"
        # 新增 universe_* 字段以默认值出现,保持向后兼容
        assert body["params"] == {
            "short_window": 10,
            "long_window": 30,
            "max_position_pct": 0.95,
            "symbol_code": None,
            "universe_mode": "all",
            "universe_lookback": 20,
            "universe_min_avg_amount": None,
            "universe_min_momentum": None,
            "universe_exit_clear": False,
        }
        assert body["selection"]["enabled"] is False
        preset_id = body["id"]

        listed = client.get("/api/strategy-presets")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [preset_id]

        detail = client.get(f"/api/strategy-presets/{preset_id}")
        assert detail.status_code == 200
        assert detail.json()["name"] == "中期趋势"

        invalid = client.put(
            f"/api/strategy-presets/{preset_id}",
            json={"params": {"short_window": 50, "long_window": 20}},
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"][0]["loc"][0] == "params"

        updated = client.put(
            f"/api/strategy-presets/{preset_id}",
            json={
                "name": "长期趋势",
                "params": {"short_window": 20, "long_window": 60},
                "selection": {
                    "enabled": True,
                    "max_symbols": 8,
                    "ranking_factor": "pb",
                    "ranking_ascending": True,
                },
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "长期趋势"
        assert updated.json()["params"]["long_window"] == 60
        assert updated.json()["selection"]["enabled"] is True
        assert updated.json()["selection"]["ranking_factor"] == "pb"

        deleted = client.delete(f"/api/strategy-presets/{preset_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/strategy-presets/{preset_id}").status_code == 404


def test_strategy_preset_rejects_unknown_strategy_and_duplicate_name(
    client: TestClient,
) -> None:
    with patch(
        "finboard_api.routes.strategy_presets.StrategyPresetRepository",
        FakeStrategyPresetRepository,
    ):
        unknown = client.post(
            "/api/strategy-presets",
            json={"name": "不安全", "strategy": "custom_python", "params": {}},
        )
        assert unknown.status_code == 422
        assert unknown.json()["detail"][0]["loc"] == ["strategy"]

        first = client.post(
            "/api/strategy-presets",
            json={
                "name": "重复名称",
                "strategy": "ma_cross",
                "params": {},
            },
        )
        assert first.status_code == 201

        duplicate = client.post(
            "/api/strategy-presets",
            json={
                "name": "重复名称",
                "strategy": "ma_cross",
                "params": {},
            },
        )
        assert duplicate.status_code == 409
