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
from typing import cast
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
