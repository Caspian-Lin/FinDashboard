"""Data + Backtest API 路由单元测试。

不需要 PostgreSQL / kernel —— data 和 backtest 路由不依赖交易内核。
使用 fastapi.TestClient 直接测试路由处理器。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_tushare_bulk_scope_rejects_etf(self) -> None:
        """Tushare 股票批量任务不能静默包含 ETF(scope 校验迁移到 executor, #144)。"""
        from types import SimpleNamespace

        from finboard_backtest.background_jobs.contracts import ExecutorError
        from finboard_backtest.background_jobs.executors.bulk_download import (
            _validate_tushare_scope,
        )

        instruments = [
            SimpleNamespace(
                code="510300.SH",
                market="a_share",
                instrument_type="etf",
            )
        ]

        with pytest.raises(ExecutorError) as exc_info:
            _validate_tushare_scope("tushare", instruments)
        assert "支持 A 股股票、可转债与指数" in exc_info.value.summary

    def test_tushare_bulk_scope_accepts_a_share_stock(self) -> None:
        """Tushare 股票批量任务接受纯 A 股股票集合(scope 校验迁移到 executor, #144)。"""
        from types import SimpleNamespace

        from finboard_backtest.background_jobs.executors.bulk_download import (
            _validate_tushare_scope,
        )

        instruments = [
            SimpleNamespace(
                code="000001.SZ",
                market="a_share",
                instrument_type="stock",
            )
        ]

        _validate_tushare_scope("tushare", instruments)

    @pytest.mark.asyncio
    async def test_tushare_lifecycle_events_use_idempotent_insert(self) -> None:
        """停复牌事件按数据库唯一键幂等写入并返回实际新增数。"""
        from types import SimpleNamespace

        from sqlalchemy.dialects import postgresql

        from finboard_api.routes.data import _persist_tushare_lifecycle_events

        event = SimpleNamespace(
            symbol="000001.SZ",
            event_type="suspension_day",
            effective_date=date(2024, 1, 3),
            suspend_timing=None,
        )
        scalar_result = MagicMock()
        scalar_result.all.return_value = [123]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalar_result
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=execute_result)

        inserted = await _persist_tushare_lifecycle_events(mock_session, [event])

        assert inserted == 1
        assert mock_session.execute.await_args is not None
        statement = mock_session.execute.await_args.args[0]
        compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
        assert "ON CONFLICT ON CONSTRAINT uq_instrument_lifecycle_event DO NOTHING" in compiled
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_instrument_summary_groups_status_market_and_type(self) -> None:
        """标的汇总接口返回数量口径所需的三组分布。"""
        from finboard_api.routes.data import summarize_instruments

        status_result = MagicMock()
        status_result.all.return_value = [("active", 3), ("delisted", 1)]
        market_result = MagicMock()
        market_result.all.return_value = [("a_share", 4)]
        type_result = MagicMock()
        type_result.all.return_value = [("stock", 3), ("etf", 1)]
        board_result = MagicMock()
        board_result.all.return_value = [("sse_main", 2), ("chinext", 1), ("unknown", 1)]
        active_etf_result = MagicMock()
        active_etf_result.scalar_one.return_value = 1
        mock_session = MagicMock()
        mock_session.execute = AsyncMock(
            side_effect=[
                status_result,
                market_result,
                type_result,
                board_result,
                active_etf_result,
            ]
        )

        result = await summarize_instruments(session=mock_session)

        assert result.total == 4
        assert result.active_total == 3
        assert result.active_etf_total == 1
        assert result.by_status == {"active": 3, "delisted": 1}
        assert result.by_market == {"a_share": 4}
        assert result.by_instrument_type == {"stock": 3, "etf": 1}
        assert result.by_listing_board == {"sse_main": 2, "chinext": 1, "unknown": 1}

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

    def test_cache_status_selection_returns_all_filtered_items_and_union_range(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        """全选端点不受分页限制,并返回上市/退市友好的整体日期范围。"""
        from finboard_data.cache import CacheMetadata

        first = tmp_path / "510300.SH_1d_qfq.parquet"
        second = tmp_path / "600519.SH_1d_qfq.parquet"
        ignored = tmp_path / "510300.SH_1d_hqfq.parquet"
        for path in (first, second, ignored):
            path.touch()

        metadata = {
            first.name: CacheMetadata(
                bar_count=200,
                first_date=date(2020, 1, 2),
                last_date=date(2024, 12, 31),
                file_size=1,
            ),
            second.name: CacheMetadata(
                bar_count=100,
                first_date=date(2022, 3, 1),
                last_date=date(2023, 6, 30),
                file_size=1,
            ),
        }

        async def metadata_for_path(path: Path) -> CacheMetadata:
            return metadata[path.name]

        with (
            patch("finboard_api.routes.data._CACHE_DIR", str(tmp_path)),
            patch(
                "finboard_data.cache.ParquetCache.metadata",
                new=AsyncMock(side_effect=metadata_for_path),
            ),
        ):
            resp = client.get("/api/data/status-selection?period=1d&adjust=qfq")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 2
        assert [item["symbol"] for item in payload["items"]] == [
            "510300.SH",
            "600519.SH",
        ]
        assert payload["first_date"] == "2020-01-02"
        assert payload["last_date"] == "2024-12-31"

        with (
            patch("finboard_api.routes.data._CACHE_DIR", str(tmp_path)),
            patch(
                "finboard_data.cache.ParquetCache.metadata",
                new=AsyncMock(side_effect=metadata_for_path),
            ),
        ):
            filtered = client.get(
                "/api/data/status-selection?period=1d&adjust=qfq&listing_board=sse_main"
            )
        assert filtered.status_code == 200
        assert [item["symbol"] for item in filtered.json()["items"]] == ["600519.SH"]

    def test_sync_enqueues_data_sync_job(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """sync 端点迁移到统一队列:返回 202 + job_id(#144)。"""
        from datetime import date as _d

        from finboard_api.deps import get_db_session

        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        fake_row = SimpleNamespace(
            job_id="BJ-TESTSYNC1",
            kind="data_sync",
            queue="data",
            status="queued",
            priority=0,
            payload={"as_of": _d(2026, 8, 13).isoformat()},
            payload_checksum="x" * 64,
            idempotency_key="data_sync:2026-08-13",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:sync",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=AsyncMock(return_value=(fake_row, True)),
        ):
            resp = client.post("/api/data/sync")
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == "BJ-TESTSYNC1"
        assert body["kind"] == "data_sync"

    def test_quality_repair_enqueues_quality_repair_job(
        self, client: TestClient, app: FastAPI
    ) -> None:
        """quality/repair 端点迁移到统一队列:返回 202 + job_id(#144)。"""

        class _FakeSession:
            async def __aenter__(self) -> _FakeSession:
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def commit(self) -> None:
                pass

        app.state.session_maker = lambda: _FakeSession()

        fake_row = SimpleNamespace(
            job_id="BJ-TESTREPAIR1",
            kind="quality_repair",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="quality_repair:abc:akshare:qfq",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:quality_repair",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=AsyncMock(return_value=(fake_row, True)),
        ):
            resp = client.post(
                "/api/data/quality/repair",
                json={"symbols": ["510600.SH"], "source": "akshare"},
            )

        assert resp.status_code == 202
        assert resp.json()["kind"] == "quality_repair"

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


class TestBulkDownloadEnqueueContract:
    """bulk_download 语义化端点的入队期契约(#347,#260 风格)。

    校验发生在 enqueue 之前,非法参数秒级 422,不触碰数据库;
    合法请求透传 symbols 子集并落 payload / 幂等键。
    """

    def _post(self, client: TestClient, app: FastAPI, json: dict[str, Any]) -> Any:
        from finboard_api.deps import get_db_session

        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        try:
            return client.post("/api/data/bulk-download", json=json)
        finally:
            app.dependency_overrides.clear()

    def test_unknown_source_rejected_422(
        self, client: TestClient, app: FastAPI
    ) -> None:
        resp = self._post(
            client, app, {"market": "a_share", "source": "wind", "start": "2024-01-01"}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "invalid_field_value" in detail
        assert "wind" in detail

    def test_bad_start_date_rejected_422(
        self, client: TestClient, app: FastAPI
    ) -> None:
        resp = self._post(
            client, app, {"market": "a_share", "source": "akshare", "start": "2024/01/01"}
        )
        assert resp.status_code == 422
        assert "invalid_field_value" in resp.json()["detail"]

    def test_tushare_etf_rejected_422(self, client: TestClient, app: FastAPI) -> None:
        """tushare x etf 字面量预检入队即拒(执行器 DB 行 scope 校验保留)。"""
        resp = self._post(
            client,
            app,
            {
                "market": "a_share",
                "source": "tushare",
                "start": "2024-01-01",
                "instrument_type": "etf",
            },
        )
        assert resp.status_code == 422
        assert "tushare_scope_mismatch" in resp.json()["detail"]

    def test_symbols_subset_passthrough(
        self, client: TestClient, app: FastAPI
    ) -> None:
        """symbols 子集(#347)落 payload + 幂等键带摘要。"""
        import hashlib

        from finboard_api.deps import get_db_session

        fake_row = SimpleNamespace(
            job_id="BJ-TESTSUB1",
            kind="bulk_download",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="bulk_download:test",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:bulk_download",
            created_at=datetime(2026, 9, 6, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 9, 6, tzinfo=UTC),
        )
        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        symbols = ["000001.SZ", "600000.SH", "000001.SZ"]
        digest = hashlib.sha256(
            ",".join(dict.fromkeys(symbols)).encode("utf-8")
        ).hexdigest()[:16]
        try:
            with patch(
                "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
                new=AsyncMock(return_value=(fake_row, True)),
            ) as create:
                resp = client.post(
                    "/api/data/bulk-download",
                    json={
                        "market": "a_share",
                        "source": "akshare",
                        "start": "2024-01-01",
                        "symbols": symbols,
                    },
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 202
        assert create.await_args is not None
        payload = create.await_args.kwargs["payload"]
        assert payload["symbols"] == ["000001.SZ", "600000.SH"]  # 去重保序
        idem = create.await_args.kwargs["idempotency_key"]
        assert idem == f"bulk_download:a_share:akshare:2024-01-01:all:sub:{digest}"


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

        # issue #43: universe_* 字段随 schema 暴露给前端表单
        assert "universe_mode" in param_names
        assert "universe_lookback" in param_names
        assert "universe_min_avg_amount" in param_names
        assert "universe_min_momentum" in param_names
        assert "universe_exit_clear" in param_names
        universe_mode = next(p for p in ma["params"] if p["name"] == "universe_mode")
        assert universe_mode["type"] == "string"
        assert universe_mode["default"] == "all"
        assert set(universe_mode["enum"]) == {"all", "liquidity_momentum"}

        periodic = next(s for s in strategies if s["kind"] == "periodic_query")
        assert periodic["supports_backtest"] is False

    def test_run_backtest_rejects_missing_required_fields(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """请求体层面 Pydantic 校验(缺 strategy 等),策略语义校验已迁移到 executor(#144)。"""
        from finboard_api.deps import get_db_session

        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        resp = client.post("/api/backtest/run", json={})
        app.dependency_overrides.clear()
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any("strategy" in item.get("loc", []) for item in detail)

    def test_run_backtest_enqueues_job(self, client: TestClient, app: FastAPI) -> None:
        """backtest/run 端点迁移到统一队列:返回 202 + job_id(#144)。"""
        from finboard_api.deps import get_db_session

        fake_row = SimpleNamespace(
            job_id="BJ-TESTBT1",
            kind="backtest_run",
            queue="data",
            status="queued",
            priority=0,
            payload={},
            payload_checksum="x" * 64,
            idempotency_key="backtest:ma_cross:abc",
            progress_total=0,
            progress_done=0,
            phase=None,
            result_ref=None,
            error_code=None,
            error_summary=None,
            attempt=0,
            max_attempts=3,
            worker_id=None,
            heartbeat_at=None,
            lease_until=None,
            requested_by="api:backtest_run",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        mock_session = AsyncMock()
        app.dependency_overrides[get_db_session] = lambda: mock_session
        with patch(
            "finboard_api.job_helpers.BackgroundJobRepository.create_or_get",
            new=AsyncMock(return_value=(fake_row, True)),
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

        assert resp.status_code == 202
        body = resp.json()
        assert body["kind"] == "backtest_run"
        assert body["job_id"] == "BJ-TESTBT1"
        app.dependency_overrides.clear()


class TestBacktestHistoryDetailRoutes:
    """GET /api/backtest/history/{run_id} 裁剪参数(issue #206/#258 REST parity)。

    REST 默认全量(与裁剪参数引入前的行为一致,前端零改动);
    selection_snapshots=none|summary|full 与 MCP history_get 同契约。
    """

    def _history_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=7,
            strategy="ma_cross",
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
            capital=Decimal("100000"),
            adjust="qfq",
            params={"short_window": 5, "long_window": 20},
            selection={"enabled": False},
            metrics={"total_return": 0.12},
            equity_curve=[
                {"date": f"day-{i:04d}", "equity": 100000.0 + i} for i in range(300)
            ],
            fills=[
                {
                    "date": "2024-01-02",
                    "symbol": "000001",
                    "side": "buy",
                    "quantity": "100",
                    "price": "10.0",
                    "commission": "1",
                }
                for _ in range(30)
            ],
            summary="total return 12%",
            selection_snapshots=[
                {
                    "decision_at": f"2024-01-{day:02d}T16:00:00+00:00",
                    "business_date": f"2024-01-{day:02d}",
                    "effective_date": f"2024-01-{day + 1:02d}",
                    "selected_symbols": ["000001", "000002"],
                    "status": "published",
                    "skip_reason": None,
                    "dataset_versions": {"a_share_tushare": "rel-1"},
                    "factor_version": "v1",
                    "checksum": f"ck-{day}",
                    "warnings": [],
                }
                for day in range(1, 4)
            ],
            dataset_versions={"a_share_tushare": ["rel-1"]},
            factor_version="v1",
            matching_model={},
            asset_rules=None,
            fee_assumptions={},
            benchmark_config={},
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

    def _get_history(
        self, client: TestClient, app: FastAPI, **params: Any
    ) -> Any:
        from finboard_api.deps import get_db_session

        row = self._history_row()
        mock_session = AsyncMock()
        app.dependency_overrides[get_db_session] = lambda: mock_session
        try:
            with patch(
                "finboard_persistence.BacktestRunRepository.get",
                new=AsyncMock(return_value=row),
            ):
                return client.get(
                    "/api/backtest/history/7",
                    params=params if params else None,
                )
        finally:
            app.dependency_overrides.clear()

    def test_default_returns_full(self, client: TestClient, app: FastAPI) -> None:
        """REST 默认全量:equity/fills/snapshots 与历史行为一致,附计数元信息。"""
        resp = self._get_history(client, app)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["equity_curve"]) == 300
        assert len(body["fills"]) == 30
        assert len(body["selection_snapshots"]) == 3
        assert body["selection_snapshots"][0]["selected_symbols"] == ["000001", "000002"]
        assert body["equity_point_count"] == 300
        assert body["fills_total"] == 30
        assert body["fills_offset"] == 0
        assert body["selection_snapshot_count"] == 3

    def test_selection_none_returns_count_only(
        self, client: TestClient, app: FastAPI
    ) -> None:
        resp = self._get_history(client, app, selection_snapshots="none")
        assert resp.status_code == 200
        body = resp.json()
        assert body["selection_snapshots"] == []
        assert body["selection_snapshot_count"] == 3
        # 其他字段不受该开关影响。
        assert len(body["equity_curve"]) == 300
        assert len(body["fills"]) == 30

    def test_selection_summary_is_count_projection(
        self, client: TestClient, app: FastAPI
    ) -> None:
        resp = self._get_history(client, app, selection_snapshots="summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["selection_snapshot_count"] == 3
        for item in body["selection_snapshots"]:
            assert "selected_symbols" not in item
            assert item["selected_symbol_count"] == 2
            assert item["checksum"].startswith("ck-")
            assert item["effective_date"].startswith("2024-01-")

    def test_selection_full_explicit_same_as_default(
        self, client: TestClient, app: FastAPI
    ) -> None:
        explicit = self._get_history(client, app, selection_snapshots="full").json()
        default = self._get_history(client, app).json()
        assert explicit["selection_snapshots"] == default["selection_snapshots"]

    def test_invalid_selection_mode_422(self, client: TestClient, app: FastAPI) -> None:
        resp = self._get_history(client, app, selection_snapshots="compact")
        assert resp.status_code == 422
        assert "selection_snapshots" in resp.json()["detail"]

    def test_equity_and_fills_parity(self, client: TestClient, app: FastAPI) -> None:
        """#206 parity:equity_mode 与 fills 分页参数可用。"""
        resp = self._get_history(
            client, app, equity_mode="none", fills_limit=5, fills_offset=10
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["equity_curve"] == []
        assert body["equity_point_count"] == 300
        assert len(body["fills"]) == 5
        assert body["fills_total"] == 30
        assert body["fills_offset"] == 10

    def test_not_found(self, client: TestClient, app: FastAPI) -> None:
        from finboard_api.deps import get_db_session

        mock_session = AsyncMock()
        app.dependency_overrides[get_db_session] = lambda: mock_session
        try:
            with patch(
                "finboard_persistence.BacktestRunRepository.get",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/backtest/history/999")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 404
