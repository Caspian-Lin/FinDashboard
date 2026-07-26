"""Data + Backtest API 路由单元测试。

不需要 PostgreSQL / kernel —— data 和 backtest 路由不依赖交易内核。
使用 fastapi.TestClient 直接测试路由处理器。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.routes import backtest_router, data_router


@pytest.fixture
def app() -> FastAPI:
    """最小化 app:只挂 data + backtest 路由。"""
    a = FastAPI()
    a.include_router(data_router)
    a.include_router(backtest_router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestDataRoutes:
    """Data 路由测试。"""

    def test_list_cache_status_empty(self, client: TestClient) -> None:
        """空缓存目录返回空列表。"""
        with patch("finboard_api.routes.data.Path") as mock_path_cls:
            mock_path_inst = mock_path_cls.return_value
            mock_path_inst.glob.return_value = []
            resp = client.get("/api/data/status")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_cache_status_page_empty(self, client: TestClient) -> None:
        """分页状态接口应返回总数,避免前端请求全部缓存详情。"""
        with patch("finboard_api.routes.data.Path") as mock_path_cls:
            mock_path_cls.return_value.glob.return_value = []
            resp = client.get("/api/data/status-page")

        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0, "limit": 200, "offset": 0}

    def test_fetch_all_updates_cache_without_materializing_bars(
        self, client: TestClient
    ) -> None:
        """旧 fetch-all 入口也必须走仅返回 bool 的缓存更新路径。"""
        from finboard_data import SymbolEntry, SymbolPoolConfig
        from finboard_data.cache import CacheMetadata

        config = SymbolPoolConfig(symbols=[SymbolEntry(code="510300.SH")])
        provider = AsyncMock()
        provider.update_cache_batch.return_value = {"510300.SH": True}
        metadata = CacheMetadata(
            bar_count=2,
            first_date=date(2024, 1, 2),
            last_date=date(2024, 1, 3),
            file_size=1024,
        )

        with (
            patch("finboard_data.load_symbol_pool", return_value=config),
            patch("finboard_api.routes.data._get_provider", return_value=provider),
            patch(
                "finboard_data.cache.ParquetCache.metadata_for",
                new=AsyncMock(return_value=metadata),
            ),
        ):
            resp = client.post("/api/data/fetch-all")

        assert resp.status_code == 200
        assert resp.json()["success"] == 1
        assert resp.json()["details"][0]["bar_count"] == 2
        provider.update_cache_batch.assert_awaited_once()
        provider.fetch_bars_batch.assert_not_awaited()

    def test_get_symbol_pool_empty(self, client: TestClient) -> None:
        """标的池不存在时返回默认配置。"""
        from finboard_data.symbols import SymbolPoolConfig

        with patch("finboard_data.load_symbol_pool") as mock_load:
            mock_load.return_value = SymbolPoolConfig()
            resp = client.get("/api/data/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbols"] == []
        assert data["fetch_period"] == "D1"

    def test_update_symbol_pool(self, client: TestClient) -> None:
        """更新标的池配置。"""
        with patch("finboard_data.save_symbol_pool") as mock_save:
            resp = client.put(
                "/api/data/symbols",
                json={
                    "symbols": [
                        {"code": "510300.SH", "name": "沪深300ETF"},
                    ],
                    "fetch_period": "D1",
                    "fetch_lookback_days": 10,
                    "fetch_adjust": "qfq",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["symbols"]) == 1
        assert data["symbols"][0]["code"] == "510300.SH"
        assert data["fetch_lookback_days"] == 10
        mock_save.assert_called_once()


class TestBacktestRoutes:
    """Backtest 路由测试。"""

    def test_list_strategies(self, client: TestClient) -> None:
        resp = client.get("/api/backtest/strategies")
        assert resp.status_code == 200
        strategies = resp.json()
        kinds = [s["kind"] for s in strategies]
        assert "ma_cross" in kinds
        assert "periodic_query" in kinds

        # 验证 ma_cross 参数信息
        ma = next(s for s in strategies if s["kind"] == "ma_cross")
        param_names = [p["name"] for p in ma["params"]]
        assert "short_window" in param_names
        assert "long_window" in param_names
        short_window = next(p for p in ma["params"] if p["name"] == "short_window")
        assert short_window["type"] == "integer"
        assert short_window["minimum"] == 1
        assert short_window["maximum"] == 250
        assert short_window["label"] == "短期均线"
        assert ma["supports_backtest"] is True

        periodic = next(s for s in strategies if s["kind"] == "periodic_query")
        assert periodic["supports_backtest"] is False

    @pytest.mark.parametrize(
        ("strategy", "params", "expected_type"),
        [
            (
                "ma_cross",
                {"short_window": 20, "long_window": 5},
                "value_error",
            ),
            (
                "ma_cross",
                {"short_window": 5, "long_window": 20, "unknown": True},
                "extra_forbidden",
            ),
            ("periodic_query", {}, "value_error.unsupported_backtest"),
        ],
    )
    def test_run_backtest_rejects_invalid_strategy_params(
        self,
        client: TestClient,
        app: FastAPI,
        strategy: str,
        params: dict[str, object],
        expected_type: str,
    ) -> None:
        from finboard_api.deps import get_db_session

        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        resp = client.post(
            "/api/backtest/run",
            json={
                "strategy": strategy,
                "symbols": ["510300.SH"],
                "start": "2024-01-01",
                "end": "2024-06-01",
                "params": params,
            },
        )
        app.dependency_overrides.clear()

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any(expected_type in item["type"] for item in detail)

    def test_run_backtest_with_mock(self, client: TestClient, app: FastAPI) -> None:
        """使用 mock BacktestEngine 验证响应结构。"""
        from finboard_api.deps import get_db_session
        from finboard_backtest.result import BacktestResult

        mock_result = BacktestResult(
            equity_curve=[(date(2024, 1, 1), Decimal("100000"))],
            benchmark_curve=[(date(2024, 1, 1), Decimal("100000"))],
            total_return=0.05,
            annualized_return=0.12,
            sharpe_ratio=1.5,
            max_drawdown=0.03,
            win_rate=0.6,
            trade_count=5,
            turnover=1.2,
            commission_paid=Decimal("15"),
            stamp_tax_paid=Decimal("10"),
            benchmark_return=0.03,
            excess_return=0.02,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 1),
            initial_capital=Decimal("100000"),
            final_equity=Decimal("105000"),
        )

        mock_engine = AsyncMock()
        mock_engine.run.return_value = mock_result

        # mock DB session —— 回测运行后落库,单元测试不依赖真实 PG
        from unittest.mock import MagicMock

        mock_session = AsyncMock()
        mock_session.add = MagicMock()  # add 是同步方法
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        app.dependency_overrides[get_db_session] = lambda: mock_session

        with (
            patch("finboard_backtest.BacktestEngine", return_value=mock_engine),
            patch("finboard_app.strategies.create_strategy"),
            patch("finboard_data.AkShareProvider"),
        ):
            resp = client.post(
                "/api/backtest/run",
                json={
                    "strategy": "ma_cross",
                    "symbols": ["510300.SH"],
                    "start": "2024-01-01",
                    "end": "2024-06-01",
                    "capital": "100000",
                    "params": {"short_window": 5, "long_window": 20},
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["metrics"]["total_return"] == 0.05
        assert data["metrics"]["sharpe_ratio"] == 1.5
        assert len(data["equity_curve"]) == 1
        assert data["equity_curve"][0]["equity"] == 100000.0
        assert data["equity_curve"][0]["benchmark"] == 100000.0
        assert "回测报告" in data["summary"]
        # 落库被调用
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()
        app.dependency_overrides.clear()
