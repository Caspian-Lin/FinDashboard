"""MCP 回测返回体积控制单元测试(issue #172 / #258)。

验证 equity 降采样(首末点保留 / 时序单调 / 点数上限)与 ``full`` 模式
回归、history_get fills 分页与元信息字段、逐决策选股快照裁剪
(``selection_snapshots=none|summary|full``,默认 none)与 run 同步响应
对齐;不依赖 PostgreSQL。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
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


def _selection_snapshots(count: int) -> list[dict[str, Any]]:
    """逐决策选股快照(落库 JSON 形态;selected_symbols 是体积膨胀点)。"""
    return [
        {
            "decision_at": f"2024-01-{day:02d}T16:00:00+00:00",
            "business_date": f"2024-01-{day:02d}",
            "effective_date": f"2024-01-{day + 1:02d}",
            "selected_symbols": ["000001", "000002", "000003"][: (day % 3) + 1],
            "status": "published",
            "skip_reason": None,
            "dataset_versions": {"a_share_tushare": "rel-1"},
            "factor_version": "v1",
            "checksum": f"ck-{day}",
            "warnings": [],
        }
        for day in range(1, count + 1)
    ]


def _history_row(
    *,
    equity_curve: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
    selection_snapshots: list[dict[str, Any]] | None = None,
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
        selection_snapshots=(
            selection_snapshots if selection_snapshots is not None else []
        ),
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

    def test_none_mode_returns_empty(self) -> None:
        """issue #190:equity_mode=none(默认)不返回任何曲线点。"""
        points = _equity_points(500)
        sampled = downsample.apply_equity_mode(
            points, equity_mode="none", max_points=10
        )
        assert sampled == []
        assert downsample.resolve_equity_mode("none") == "none"

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


# ---------------------------------------------------------------------------
# 逐决策选股快照裁剪(issue #258)
# ---------------------------------------------------------------------------


class TestApplySelectionMode:
    """纯函数:summary 投影 / full 全量 / none 空。"""

    def test_none_returns_empty(self) -> None:
        assert downsample.apply_selection_mode(
            _selection_snapshots(3), selection_mode="none"
        ) == []

    def test_full_returns_all(self) -> None:
        rows = _selection_snapshots(3)
        assert downsample.apply_selection_mode(rows, selection_mode="full") == rows

    def test_summary_projects_count_without_symbol_list(self) -> None:
        rows = _selection_snapshots(3)
        projected = downsample.apply_selection_mode(rows, selection_mode="summary")
        assert len(projected) == 3
        for original, item in zip(rows, projected, strict=True):
            assert set(item) == {
                "decision_at",
                "business_date",
                "effective_date",
                "status",
                "skip_reason",
                "checksum",
                "selected_symbol_count",
            }
            assert "selected_symbols" not in item
            assert item["selected_symbol_count"] == len(original["selected_symbols"])
            # checksum 与 full 同名同值,消费方可用它稳定键控单条快照。
            assert item["checksum"] == original["checksum"]

    def test_summary_tolerates_missing_symbols_key(self) -> None:
        projected = downsample.apply_selection_mode(
            [{"business_date": "2024-01-02"}], selection_mode="summary"
        )
        assert projected[0]["selected_symbol_count"] == 0

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="selection_snapshots"):
            downsample.resolve_selection_mode("compact")


class TestHistoryGetSelectionSnapshots:
    """history_get 三种模式序列化断言 + 默认值回归(issue #258)。"""

    async def _get(
        self,
        monkeypatch: pytest.MonkeyPatch,
        row: MagicMock,
        **kwargs: Any,
    ) -> Any:
        app = _make_app()
        monkeypatch.setattr(
            BacktestRunRepository, "get", lambda self, run_id: _async_return(row)
        )
        return await backtest_tools.backtest_history_get(app, run_id=1, **kwargs)

    async def test_default_none_returns_count_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认 none:不回快照列表,只回计数元数据(#258 主诉求)。"""
        env = await self._get(
            monkeypatch, _history_row(selection_snapshots=_selection_snapshots(4))
        )
        assert env.status == "ok"
        data = env.data
        assert data["selection_snapshots"] == []
        assert data["selection_snapshot_count"] == 4
        # 其余字段不受影响。
        assert data["equity_point_count"] == 500

    async def test_empty_history_count_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await self._get(monkeypatch, _history_row(selection_snapshots=[]))
        assert env.data["selection_snapshots"] == []
        assert env.data["selection_snapshot_count"] == 0

    async def test_summary_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _selection_snapshots(3)
        env = await self._get(
            monkeypatch, _history_row(selection_snapshots=rows),
            selection_snapshots="summary",
        )
        assert env.status == "ok"
        projected = env.data["selection_snapshots"]
        assert len(projected) == 3
        for item in projected:
            assert "selected_symbols" not in item
            assert item["selected_symbol_count"] == len(
                next(r for r in rows if r["checksum"] == item["checksum"])[
                    "selected_symbols"
                ]
            )
        assert env.data["selection_snapshot_count"] == 3

    async def test_full_returns_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _selection_snapshots(3)
        env = await self._get(
            monkeypatch, _history_row(selection_snapshots=rows),
            selection_snapshots="full",
        )
        assert env.data["selection_snapshots"] == rows
        assert env.data["selection_snapshots"][0]["selected_symbols"] == [
            "000001",
            "000002",
        ]
        assert env.data["selection_snapshots"][0]["warnings"] == []

    async def test_invalid_mode_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await self._get(
            monkeypatch, _history_row(), selection_snapshots="compact"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# backtest_run 同步响应选股快照对齐(issue #258)
# ---------------------------------------------------------------------------


def _fake_selection_rows(count: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            decision_at=datetime(2024, 1, day, tzinfo=UTC),
            business_date=date(2024, 1, day),
            effective_date=date(2024, 1, day + 1),
            selected_symbols=["000001", "000002"],
            status=SimpleNamespace(value="published"),
            skip_reason=None,
            dataset_versions={},
            factor_version="v1",
            checksum=f"ck-{day}",
            warnings=[],
        )
        for day in range(1, count + 1)
    ]


def _fake_engine_result(selection_rows: list[SimpleNamespace]) -> SimpleNamespace:
    result = SimpleNamespace(
        equity_curve=[(date(2024, 1, 2), 100000.0), (date(2024, 1, 31), 101000.0)],
        benchmark_curve=[],
        fills=[],
        total_return=0.01,
        annualized_return=0.12,
        sharpe_ratio=1.0,
        sharpe_rf0=1.1,
        risk_free_annual=0.03,
        max_drawdown=-0.05,
        win_rate=0.5,
        trade_count=0,
        turnover=0.2,
        commission_paid=Decimal("5"),
        stamp_tax_paid=Decimal("0"),
        benchmark_return=None,
        excess_return=None,
        benchmark_source=None,
        selection_diagnostics={},
        initial_capital=Decimal("100000"),
        final_equity=Decimal("101000"),
        dataset_versions={},
        factor_version="v1",
        matching_model={},
        asset_rules=None,
        fee_assumptions={},
        benchmark_config=None,
        selection_snapshots=selection_rows,
    )
    result.summary = MagicMock(return_value="total return 1%")
    return result


class TestRunSyncSelectionMode:
    """strategy 形态同步响应与 history_get 同一裁剪开关。"""

    async def _run_sync(
        self, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
    ) -> Any:
        app = _make_app()
        engine_cls = MagicMock()
        engine_cls.return_value.run = AsyncMock(
            return_value=_fake_engine_result(_fake_selection_rows(2))
        )
        monkeypatch.setattr("finboard_backtest.BacktestEngine", engine_cls)
        monkeypatch.setattr(
            "finboard_backtest.providers.build_backtest_bar_provider",
            lambda *args, **kw: MagicMock(),
        )
        monkeypatch.setattr(BacktestRunRepository, "save", AsyncMock())
        return await backtest_tools.backtest_run(
            app,
            strategy="ma_cross",
            symbols=["000001"],
            start="2024-01-02",
            end="2024-01-31",
            params={"short_window": 5, "long_window": 20},
            **kwargs,
        )

    async def test_default_none_returns_count_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await self._run_sync(monkeypatch)
        assert env.status == "ok"
        data = env.data
        assert data["selection_snapshots"] == []
        assert data["selection_snapshot_count"] == 2

    async def test_full_returns_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = await self._run_sync(monkeypatch, selection_snapshots="full")
        assert env.status == "ok"
        snapshots = env.data["selection_snapshots"]
        assert len(snapshots) == 2
        assert snapshots[0]["selected_symbols"] == ["000001", "000002"]
        assert env.data["selection_snapshot_count"] == 2

    async def test_summary_projection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = await self._run_sync(monkeypatch, selection_snapshots="summary")
        assert env.status == "ok"
        for item in env.data["selection_snapshots"]:
            assert "selected_symbols" not in item
            assert item["selected_symbol_count"] == 2

    async def test_invalid_mode_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await self._run_sync(monkeypatch, selection_snapshots="compact")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
