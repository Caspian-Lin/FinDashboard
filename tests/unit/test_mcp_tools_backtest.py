"""``finboard.backtest.*`` 工具 —— 回测引擎工具测试(mock,无需 DB)。

覆盖:

* 只读:strategy_list(无 DB,直接验证 registry)/ history_list /
  history_get(含 not_found)
* 写:run(验证 write_disabled + invalid strategy)、history_delete
  (write_disabled + not_found)
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
# finboard.backtest.strategy_list(无 DB)
# ---------------------------------------------------------------------------


class TestBacktestStrategyList:
    async def test_returns_only_backtest_capable_strategies(self) -> None:
        app = _make_app()
        env = await backtest_tools.backtest_strategy_list(app)
        assert env.status == "ok"
        data = env.data
        assert isinstance(data, list)
        # ma_cross 支持, periodic_query / etf_dca 不支持
        kinds = [item["kind"] for item in data]
        assert "ma_cross" in kinds
        assert "periodic_query" not in kinds
        assert "etf_dca" not in kinds


# ---------------------------------------------------------------------------
# finboard.backtest.run(写)
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
