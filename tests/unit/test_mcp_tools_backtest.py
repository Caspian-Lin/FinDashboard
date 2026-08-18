"""``finboard.backtest.*`` 工具 —— 回测引擎工具测试(mock,无需 DB)。

覆盖:

* 只读:strategy_list(内置策略 + 已发布规格,issue #174)/ history_list /
  history_get(含 not_found)
* 写:run(验证 write_disabled + invalid strategy + strategy_spec 路由/互斥)、
  history_delete(write_disabled + not_found)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import backtest as backtest_tools

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _async_return(value: object) -> object:
    return value


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _get_session(app: McpAppContext) -> AsyncMock:
    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _spec_row(
    strategy_id: str = "mf_test",
    version: int = 1,
    status: str = "published",
) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id=strategy_id,
        version=version,
        strategy_kind="etf",
        name="多因子测试",
        status=status,
        payload={"schema_version": "v1"},
    )


def _history_row(run_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        strategy="ma_cross",
        symbols=["000001"],
        start="2024-01-01",
        end="2024-06-30",
        capital=Decimal("100000"),
        adjust="qfq",
        params={"short_window": 5, "long_window": 20},
        selection={"enabled": False},
        metrics={"total_return": 0.12},
        factor_version="v1",
        equity_curve=[{"date": "2024-01-01", "equity": 100000.0}],
        fills=[],
        summary="total return 12%",
        selection_snapshots=[],
        dataset_versions={},
        matching_model={},
        asset_rules=None,
        fee_assumptions={},
        benchmark_config={},
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# finboard.backtest.strategy_list(只读,issue #174 双形态输出)
# ---------------------------------------------------------------------------


class TestBacktestStrategyList:
    async def test_returns_builtin_and_published_specs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_published",
            lambda self, **kw: _async_return([_spec_row(), _spec_row("s2", 3)]),
        )
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_history",
            lambda self, strategy_id: _async_return(
                [_spec_row(strategy_id, 1), _spec_row(strategy_id, 2)]
            ),
        )
        env = await backtest_tools.backtest_strategy_list(app)
        assert env.status == "ok"
        data = env.data
        assert isinstance(data, dict)
        # 内置事件驱动策略:仅 ma_cross 支持回测
        builtin = data["builtin_strategies"]
        kinds = [item["kind"] for item in builtin]
        assert "ma_cross" in kinds
        assert "periodic_query" not in kinds
        assert "etf_dca" not in kinds
        # 已发布规格:含状态 / 版本数 / 执行入口提示
        specs = data["published_specs"]
        assert len(specs) == 2
        assert specs[0]["strategy_id"] == "mf_test"
        assert specs[0]["status"] == "published"
        assert specs[0]["version"] == 1
        assert specs[0]["version_count"] == 2
        assert "backtest_run(strategy_spec=" in specs[0]["execution_hint"]
        assert "execution_note" in data

    async def test_empty_published_specs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "list_published",
            lambda self, **kw: _async_return([]),
        )
        env = await backtest_tools.backtest_strategy_list(app)
        assert env.status == "ok"
        data = env.data
        assert data["published_specs"] == []
        assert data["builtin_strategies"]


# ---------------------------------------------------------------------------
# finboard.backtest.run(写,双形态)
# ---------------------------------------------------------------------------


class TestBacktestRun:
    async def test_write_disabled_returns_permission_denied(self) -> None:
        app = _make_app(write_enabled=False)
        env = await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_write_disabled_strategy_spec_also_denied(self) -> None:
        app = _make_app(write_enabled=False)
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test", "version": 1},
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_strategy_returns_invalid_argument(self) -> None:
        app = _make_app()
        env = await backtest_tools.backtest_run(
            app,
            strategy="unknown_kind",
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_missing_both_forms_returns_invalid_argument(self) -> None:
        app = _make_app()
        env = await backtest_tools.backtest_run(app)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    # -- strategy_spec 路由(issue #174)--

    async def test_strategy_spec_and_strategy_mutually_exclusive(self) -> None:
        app = _make_app()
        env = await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            strategy_spec={"strategy_id": "mf_test", "version": 1},
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "互斥" in env.error.message

    async def test_strategy_spec_missing_version(self) -> None:
        app = _make_app()
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test"},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_strategy_spec_version_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, strategy_id, version: _async_return(None),
        )
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test", "version": 9},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "不存在" in env.error.message

    async def test_strategy_spec_not_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, strategy_id, version: _async_return(
                _spec_row(status="draft")
            ),
        )
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test", "version": 1},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "已发布" in env.error.message

    async def test_strategy_spec_routes_to_research_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路由成功:校验 published 后复用 run_queue 入队,返回 run/job 指针。"""
        from finboard_mcp.tools import runs as runs_tools
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, strategy_id, version: _async_return(_spec_row()),
        )
        fake_detail = {
            "run_id": "RR-abc123",
            "job_id": "BJ-xyz",
            "status": "queued",
            "strategy_id": "mf_test",
            "strategy_kind": "etf",
            "manifest_checksum": "mc",
        }
        enqueue_mock = AsyncMock(return_value=fake_detail)
        monkeypatch.setattr(runs_tools, "enqueue_research_run", enqueue_mock)
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test", "version": 1},
            queue_payload={
                "idempotency_key": "key-12345678",
                "dataset_release_ids": ["REL-1"],
                "code_version": "v1.2.3.4",
                "initial_capital": "200000",
                "requested_by": "tester",
            },
        )
        assert env.status == "ok"
        data = env.data
        assert data["run_id"] == "RR-abc123"
        assert data["job_id"] == "BJ-xyz"
        assert data["status"] == "queued"
        assert "research_run" in data["execution_path"]
        # strategy_id/version 以 strategy_spec 为准注入已解析的 queue body
        assert enqueue_mock.await_args is not None
        body = enqueue_mock.await_args.args[1]
        assert body.strategy_id == "mf_test"
        assert body.strategy_version == 1

    async def test_strategy_spec_invalid_queue_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_persistence import ResearchStrategySpecRepository

        app = _make_app()
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, strategy_id, version: _async_return(_spec_row()),
        )
        env = await backtest_tools.backtest_run(
            app,
            strategy_spec={"strategy_id": "mf_test", "version": 1},
            queue_payload={"dataset_release_ids": ["REL-1"]},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# 策略形态异步化(issue #189):自动切换判定 + 入队返回 job_id
# ---------------------------------------------------------------------------


class TestBacktestRunAsync:
    def test_estimate_symbol_days(self) -> None:
        from finboard_mcp.tools.backtest import _estimate_symbol_days

        # 半年(2024-01-01..2024-06-30 ≈ 130 个交易日)x 1 标的
        est = _estimate_symbol_days(["000001"], "2024-01-01", "2024-06-30")
        assert 120 <= est <= 140
        # 2 标的大约线性翻倍
        est2 = _estimate_symbol_days(
            ["000001", "000002"], "2024-01-01", "2024-06-30"
        )
        assert est2 == 2 * est
        # 非法日期不抛,按 0 处理(调用方据 0 永不算达标)
        assert _estimate_symbol_days(["000001"], "bad-date", "2024-01-01") == 0

    def test_resolve_async_mode(self) -> None:
        from finboard_mcp.tools.backtest import _resolve_async_mode

        # 显式强制:run_async=true / false 覆盖自动判定
        explicit = _resolve_async_mode(
            True, 0, ["000001"], "2024-01-01", "2024-06-30"
        )
        assert explicit.use_async is True
        assert explicit.reason == "explicit"
        forced_sync = _resolve_async_mode(
            False, 10**9, ["000001"], "2024-01-01", "2024-06-30"
        )
        assert forced_sync.use_async is False
        assert forced_sync.reason == "sync_explicit"
        # 阈值 0 = 关闭自动切换,省略 run_async 时恒同步
        disabled = _resolve_async_mode(
            None, 0, ["000001"], "2024-01-01", "2024-06-30"
        )
        assert disabled.use_async is False
        assert disabled.reason == "sync_below_threshold"
        # 阈值 1 → 任意规模自动异步
        auto = _resolve_async_mode(None, 1, ["000001"], "2024-01-01", "2024-06-30")
        assert auto.use_async is True
        assert auto.reason == "auto_threshold"
        assert auto.symbol_days_estimate > 0
        # 阈值远大于估算 → 保持同步(小规模行为不变)
        under = _resolve_async_mode(
            None, 10**9, ["000001"], "2024-01-01", "2024-06-30"
        )
        assert under.use_async is False
        assert under.reason == "sync_below_threshold"

    async def test_run_async_true_enqueues_job_and_skips_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        import finboard_app.strategies as strategies_mod
        from finboard_persistence import BackgroundJobRepository

        app = _make_app()
        session = _get_session(app)
        row = SimpleNamespace(job_id="BJ-189abc", status="queued")
        create_or_get = AsyncMock(return_value=(row, True))
        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", create_or_get)

        def _engine_should_not_run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("异步路径不得进入同步引擎")

        monkeypatch.setattr(strategies_mod, "create_strategy", _engine_should_not_run)

        env = await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            symbols=["000001.SZ", "000002.SZ"],
            start="2024-01-01",
            end="2025-12-31",
            capital=Decimal("200000"),
            benchmark_symbol="000300.SH",
            run_async=True,
        )
        assert env.status == "ok", env.error
        data = env.data
        assert data["job_id"] == "BJ-189abc"
        assert data["status"] == "queued"
        assert data["created"] is True
        assert data["async_mode"] == "explicit"
        assert data["auto_async_threshold"] == 15000
        assert data["idempotency_key"].startswith("backtest:ma_cross:")
        assert "finboard_job_get" in data["execution_path"]
        session.commit.assert_awaited()

        # payload 与 REST /api/backtest/run 同形:{request, provider_name}
        assert create_or_get.await_count == 1
        call = create_or_get.await_args
        assert call is not None
        kwargs = call.kwargs
        assert kwargs["kind"] == "backtest_run"
        assert kwargs["queue"] == "data"
        assert kwargs["requested_by"] == "agent:mcp:backtest_run"
        request: dict[str, Any] = kwargs["payload"]["request"]
        assert request["strategy"] == "ma_cross"
        assert request["symbols"] == ["000001.SZ", "000002.SZ"]
        # benchmark_symbol 透传为 BacktestRunRequest.benchmark(issue #184 语义)
        assert request["benchmark"]["symbol"] == "000300.SH"
        assert request["benchmark"]["equal_weight_universe"] is True
        assert request["params"]["long_window"] == 20  # 参数校验后展开默认值

    async def test_run_async_invalid_params_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_persistence import BackgroundJobRepository

        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((SimpleNamespace(job_id="BJ-x", status="queued"), True)),
        )
        env = await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
            params={"short_window": -5, "long_window": "nope"},
            run_async=True,
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_run_async_conflict_maps_to_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        from finboard_persistence import BackgroundJobRepository

        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup"))),
        )
        env = await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            symbols=["000001"],
            start="2024-01-01",
            end="2024-06-30",
            run_async=True,
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"


# ---------------------------------------------------------------------------
# finboard.backtest.history_list / history_get / history_delete
# ---------------------------------------------------------------------------


class TestBacktestHistoryList:
    async def test_returns_history_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import BacktestRunRepository

        app = _make_app()
        rows = [_history_row(1), _history_row(2)]
        monkeypatch.setattr(
            BacktestRunRepository,
            "list_recent",
            lambda self, **kw: _async_return(rows),
        )
        env = await backtest_tools.backtest_history_list(app, limit=10)
        assert env.status == "ok"
        data = env.data
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["id"] == 1
        assert data[0]["strategy"] == "ma_cross"


class TestBacktestHistoryGet:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import BacktestRunRepository

        app = _make_app()
        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(None),
        )
        env = await backtest_tools.backtest_history_get(app, run_id=999)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_found_returns_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import BacktestRunRepository

        app = _make_app()
        row = _history_row(1)
        monkeypatch.setattr(
            BacktestRunRepository,
            "get",
            lambda self, run_id: _async_return(row),
        )
        env = await backtest_tools.backtest_history_get(app, run_id=1)
        assert env.status == "ok"
        data = env.data
        assert data["id"] == 1
        assert "equity_curve" in data
        assert "fills" in data


class TestBacktestHistoryDelete:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await backtest_tools.backtest_history_delete(app, run_id=1)
        assert env.status == "denied"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import BacktestRunRepository

        app = _make_app()
        monkeypatch.setattr(
            BacktestRunRepository,
            "delete",
            lambda self, run_id: _async_return(False),
        )
        env = await backtest_tools.backtest_history_delete(app, run_id=999)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_persistence import BacktestRunRepository

        app = _make_app()
        session = _get_session(app)
        monkeypatch.setattr(
            BacktestRunRepository,
            "delete",
            lambda self, run_id: _async_return(True),
        )
        env = await backtest_tools.backtest_history_delete(app, run_id=1)
        assert env.status == "ok"
        assert env.data["deleted"] is True
        session.commit.assert_awaited()
