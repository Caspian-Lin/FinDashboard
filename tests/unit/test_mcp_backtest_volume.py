"""MCP 回测返回体积控制单元测试(issue #172)。

验证 equity 降采样(首末点保留 / 时序单调 / 点数上限)与 ``full`` 模式
回归、history_get fills 分页与元信息字段;不依赖 PostgreSQL。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from finboard_mcp import downsample
from finboard_mcp.tools import backtest as backtest_tools
from finboard_persistence import BacktestRunRepository


def _async_return(value: Any) -> Any:
    async def _inner() -> Any:
        return value

    return _inner()


def _make_app() -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from finboard_app.config import Settings
    from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.context import McpAppContext

    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _equity_points(count: int) -> list[dict[str, Any]]:
    # day 前导零补足,保证字符串序与时间序一致(降采样单调性断言用)。
    return [
        {"date": f"day-{day:04d}", "equity": 100000.0 + day}
        for day in range(1, count + 1)
    ]


def _history_row(
    *,
    equity_curve: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
) -> MagicMock:
    return MagicMock(
        id=1,
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
        equity_curve=equity_curve or _equity_points(500),
        fills=fills or [{"symbol": "000001", "quantity": "100"} for _ in range(20)],
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
# 降采样纯函数
# ---------------------------------------------------------------------------


class TestDownsampleEquity:
    def test_first_and_last_preserved(self) -> None:
        points = _equity_points(100)
        sampled = downsample.downsample_equity(points, max_points=10)
        assert sampled[0] == points[0]
        assert sampled[-1] == points[-1]

    def test_time_order_monotonic(self) -> None:
        points = _equity_points(100)
        sampled = downsample.downsample_equity(points, max_points=10)
        dates = [item["date"] for item in sampled]
        assert dates == sorted(dates)

    def test_point_limit_honored(self) -> None:
        points = _equity_points(1000)
        sampled = downsample.downsample_equity(points, max_points=50)
        assert len(sampled) <= 50

    def test_under_limit_returns_all(self) -> None:
        points = _equity_points(5)
        sampled = downsample.downsample_equity(points, max_points=200)
        assert sampled == points

    def test_full_mode_returns_everything(self) -> None:
        points = _equity_points(500)
        sampled = downsample.apply_equity_mode(
            points, equity_mode="full", max_points=10
        )
        assert sampled == points

    def test_summary_mode_returns_limited(self) -> None:
        points = _equity_points(500)
        sampled = downsample.apply_equity_mode(
            points, equity_mode="summary", max_points=10
        )
        assert len(sampled) <= 10

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="equity_mode"):
            downsample.resolve_equity_mode("compact")


# ---------------------------------------------------------------------------
# history_get 分页与 mode
# ---------------------------------------------------------------------------


class TestHistoryGetPagination:
    async def test_default_summary_downsampled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        row = _history_row(equity_curve=_equity_points(500))
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        env = await backtest_tools.backtest_history_get(app, run_id=1)
        assert env.status == "ok"
        data = env.data
        assert len(data["equity_curve"]) <= 200
        assert data["equity_point_count"] == 500
        # 首末点保留。
        assert data["equity_curve"][0]["date"] == "day-0001"
        assert data["equity_curve"][-1]["date"] == "day-0500"

    async def test_full_mode_returns_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        row = _history_row(equity_curve=_equity_points(500))
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        env = await backtest_tools.backtest_history_get(
            app, run_id=1, equity_mode="full"
        )
        assert env.status == "ok"
        assert len(env.data["equity_curve"]) == 500

    async def test_fills_pagination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        row = _history_row(fills=[{"symbol": f"s{i}"} for i in range(20)])
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        env = await backtest_tools.backtest_history_get(
            app, run_id=1, fills_limit=5, fills_offset=10
        )
        assert env.status == "ok"
        data = env.data
        assert len(data["fills"]) == 5
        assert data["fills"][0]["symbol"] == "s10"
        assert data["fills_total"] == 20
        assert data["fills_offset"] == 10

    async def test_fills_no_limit_returns_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        row = _history_row(fills=[{"symbol": f"s{i}"} for i in range(20)])
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        env = await backtest_tools.backtest_history_get(app, run_id=1)
        assert env.status == "ok"
        assert len(env.data["fills"]) == 20
        assert env.data["fills_total"] == 20

    async def test_invalid_equity_mode_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        row = _history_row()
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        env = await backtest_tools.backtest_history_get(
            app, run_id=1, equity_mode="compact"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
