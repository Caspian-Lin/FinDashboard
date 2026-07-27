"""Research API 路由单元测试。

不依赖 PostgreSQL —— 用 mock AsyncSession 模拟。
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session
from finboard_api.routes import research_router
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ResearchExperiment,
    RobustnessPlan,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    new_experiment,
)
from finboard_persistence.models import (
    ResearchExperimentModel,
)


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(research_router)
    return a


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    # execute 返回的 ResultMock:scalar_one_or_none 默认 None(走 insert 路径)
    session.execute = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock
    return session


@pytest.fixture
def client(app: FastAPI, mock_session: AsyncMock) -> TestClient:
    app.dependency_overrides[get_db_session] = lambda: mock_session
    return TestClient(app)


def _make_experiment(
    *,
    experiment_id: str = "exp123",
    status: ExperimentStatus = ExperimentStatus.HYPOTHESIS,
) -> ResearchExperiment:
    return new_experiment(
        hypothesis="测试假设:均线交叉策略在宽基 ETF 上有 alpha,Sharpe > 1.0",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version=None,
            dataset_versions={},
            selection_config={},
            strategy_kind="ma_cross",
        ),
        plan=ValidationPlan(
            mode=ValidationMode.ROLLING,
            train_start=date(2020, 1, 1),
            train_end=date(2020, 12, 31),
            validation_start=date(2021, 1, 1),
            validation_end=date(2021, 6, 30),
            test_start=date(2021, 7, 1),
            test_end=date(2021, 12, 31),
        ),
        thresholds=AcceptanceThresholds(),
        robustness=RobustnessPlan(),
        strategy_params_space={"window": [5, 10]},
        notes="测试",
    )


def _experiment_orm_model(exp: ResearchExperiment) -> ResearchExperimentModel:
    """构造一个 ORM 模型用于 mock 返回。"""
    m = ResearchExperimentModel(
        experiment_id=exp.experiment_id,
        hypothesis=exp.hypothesis,
        version_stamp=exp.version_stamp.as_dict(),
        version_checksum=exp.version_stamp.checksum(),
        plan=exp.plan.as_dict(),
        thresholds=exp.thresholds.as_dict(),
        robustness=exp.robustness.as_dict(),
        strategy_params_space=exp.strategy_params_space,
        status=exp.status.value,
        created_at=exp.created_at,
        frozen_at=exp.frozen_at,
        finalized_at=exp.finalized_at,
        trials_used=exp.trials_used,
        final_test_unsealed=exp.final_test_unsealed,
        rejection_reason=exp.rejection_reason,
        supersedes_id=exp.supersedes_id,
        notes=exp.notes,
    )
    # SQLAlchemy ORM 需要 id 字段
    m.id = 1
    return m


def _valid_create_payload() -> dict[str, Any]:
    return {
        "hypothesis": "测试假设:均线交叉策略在宽基 ETF 上有 alpha,Sharpe > 1.0",
        "version_stamp": {
            "matching_model_version": "v2",
            "asset_rules_version": "v1",
            "factor_version": None,
            "dataset_versions": {"daily_metrics": "2024-01-01"},
            "selection_config": {},
            "strategy_kind": "ma_cross",
        },
        "plan": {
            "mode": "rolling",
            "train_start": "2020-01-01",
            "train_end": "2020-12-31",
            "validation_start": "2021-01-01",
            "validation_end": "2021-06-30",
            "test_start": "2021-07-01",
            "test_end": "2021-12-31",
            "train_window_days": 252,
            "test_window_days": 63,
            "step_days": 21,
            "trial_budget": 20,
            "random_seed": 0,
            "benchmark_symbol": None,
        },
        "thresholds": {
            "min_in_sample_sharpe": 1.0,
            "min_oos_sharpe": 0.5,
            "max_oos_drawdown": 0.25,
            "min_oos_calmar": 0.5,
            "min_oos_information_ratio": 0.0,
            "max_param_sensitivity_sharpe_drop": 0.5,
            "min_pbo_pass": True,
            "max_pbo": 0.5,
            "min_deflated_sharpe": 0.0,
            "min_probabilistic_sharpe": 0.95,
        },
        "robustness": {
            "neighbourhood_steps": 5,
            "neighbourhood_relative_step": 0.1,
            "cost_multipliers": [1.0, 2.0, 3.0],
            "slippage_stress_bps": [0.0, 5.0, 10.0, 20.0],
            "execution_delay_bars": [1, 2],
            "stress_phases": ["2018-Q4", "2020-Q1", "2022-Q1", "2024-Q1"],
        },
        "strategy_params_space": {"window": [5, 10, 20]},
        "notes": "测试",
    }


class TestCreateExperiment:
    def test_create_returns_201(
        self, client: TestClient, mock_session: AsyncMock
    ) -> None:
        resp = client.post(
            "/api/research/experiments",
            json=_valid_create_payload(),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "hypothesis"
        assert body["trials_used"] == 0
        assert body["final_test_unsealed"] is False
        assert body["experiment_id"]
        assert body["version_checksum"]
        # 落库被调用
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    def test_empty_hypothesis_rejected(self, client: TestClient) -> None:
        payload = _valid_create_payload()
        payload["hypothesis"] = "  "
        resp = client.post("/api/research/experiments", json=payload)
        # ValueError → 500
        assert resp.status_code >= 400

    def test_short_hypothesis_rejected(self, client: TestClient) -> None:
        payload = _valid_create_payload()
        payload["hypothesis"] = "短"
        resp = client.post("/api/research/experiments", json=payload)
        assert resp.status_code == 422


class TestListAndGet:
    def test_list_empty(self, client: TestClient) -> None:
        # mock_session fixture 默认返回空 list
        resp = client.get("/api/research/experiments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_missing_returns_404(self, client: TestClient) -> None:
        # mock_session fixture 默认 scalar_one_or_none 返回 None
        resp = client.get("/api/research/experiments/nonexistent")
        assert resp.status_code == 404


class TestReject:
    def test_reject_missing_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/research/experiments/nope/reject",
            json={"reason": "x"},
        )
        assert resp.status_code == 404


class TestDelete:
    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/api/research/experiments/nope")
        assert resp.status_code == 404
