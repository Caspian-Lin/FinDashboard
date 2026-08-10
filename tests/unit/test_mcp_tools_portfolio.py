"""``finboard.portfolio.*`` 工具 —— 组合计算工具测试(mock,无需 DB)。

覆盖:

* ``portfolio_allocate``:equal_weight happy path / write_disabled /
  invalid_argument(空 signals)
* ``portfolio_sizing``:happy path / write_disabled / invalid_argument(非法
  max_leverage < 1)
* ``portfolio_feasibility``:happy path(返回 3 档)/ write_disabled
* ``portfolio_attribution``:happy path / write_disabled /
  invalid_argument(空 weights_history)
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import portfolio as portfolio_tools

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    provider = FakeLLMProvider()
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or cast("async_sessionmaker[AsyncSession]", cm),
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


# ---------------------------------------------------------------------------
# portfolio_allocate
# ---------------------------------------------------------------------------


class TestPortfolioAllocate:
    async def test_equal_weight_happy_path(self) -> None:
        """equal_weight 法无协方差输入 → build_portfolio 返回等权目标权重。"""
        app = _make_app()
        env = await portfolio_tools.portfolio_allocate(
            app,
            signals=[
                {"symbol": "000001", "score": 1.0},
                {"symbol": "600000", "score": 1.0},
            ],
            as_of="2026-01-15",
            method="equal_weight",
            max_weight_per_asset=0.5,
            max_weight_per_sleeve=1.0,
            min_cash_buffer=0.0,
        )
        assert env.status == "ok"
        assert env.error is None
        data = env.data
        assert data is not None
        weight_map = {w["code"]: w["weight"] for w in data["weights"]}
        assert set(weight_map) == {"000001", "600000"}
        assert weight_map["000001"] == pytest.approx(0.5)
        assert weight_map["600000"] == pytest.approx(0.5)
        assert data["n_assets"] == 2
        assert data["gross_weight"] == pytest.approx(1.0)
        assert data["covariance_fallback_used"] is False
        assert "risk" in data
        assert "adjustments" in data

    async def test_write_disabled_returns_permission_denied(self) -> None:
        app = _make_app(write_enabled=False)
        env = await portfolio_tools.portfolio_allocate(
            app,
            signals=[{"symbol": "000001", "score": 1.0}],
            as_of="2026-01-15",
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_empty_signals_invalid_argument(self) -> None:
        app = _make_app()
        env = await portfolio_tools.portfolio_allocate(
            app,
            signals=[],
            as_of="2026-01-15",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_invalid_date_invalid_argument(self) -> None:
        app = _make_app()
        env = await portfolio_tools.portfolio_allocate(
            app,
            signals=[{"symbol": "000001", "score": 1.0}],
            as_of="not-a-date",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# portfolio_sizing
# ---------------------------------------------------------------------------


class TestPortfolioSizing:
    async def test_happy_path(self) -> None:
        """两标的等权 10 万资金 → solve_sizing 返回 trades + 费用。"""
        app = _make_app()
        env = await portfolio_tools.portfolio_sizing(
            app,
            weights={"000001": 0.5, "600000": 0.5},
            as_of="2026-01-15",
            capital=100_000.0,
            lot_info=[
                {"code": "000001", "lot_size": 100},
                {"code": "600000", "lot_size": 100},
            ],
            prices={"000001": 10.0, "600000": 20.0},
        )
        assert env.status == "ok"
        assert env.error is None
        data = env.data
        assert data is not None
        assert isinstance(data["trades"], list)
        assert len(data["trades"]) == 2
        assert data["total_capital"] == pytest.approx(100_000.0)
        assert data["n_active_trades"] >= 1
        assert "cash_after" in data
        assert "est_commission" in data
        assert "margin_required" in data

    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await portfolio_tools.portfolio_sizing(
            app,
            weights={"000001": 1.0},
            as_of="2026-01-15",
            capital=100_000.0,
            lot_info=[{"code": "000001", "lot_size": 100}],
            prices={"000001": 10.0},
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_max_leverage(self) -> None:
        """max_leverage < 1 → TargetWeight / PortfolioConstraints 抛 ValueError。"""
        app = _make_app()
        env = await portfolio_tools.portfolio_sizing(
            app,
            weights={"000001": 1.0},
            as_of="2026-01-15",
            capital=100_000.0,
            lot_info=[{"code": "000001", "lot_size": 100}],
            prices={"000001": 10.0},
            max_leverage=0.5,  # < 1 非法
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# portfolio_feasibility
# ---------------------------------------------------------------------------


class TestPortfolioFeasibility:
    async def test_happy_path_three_tiers(self) -> None:
        """固定评估 10万/20万/50万三档 → 返回 3 项。"""
        app = _make_app()
        env = await portfolio_tools.portfolio_feasibility(
            app,
            weights={"000001": 0.5, "600000": 0.5},
            as_of="2026-01-15",
            lot_info=[
                {"code": "000001", "lot_size": 100},
                {"code": "600000", "lot_size": 100},
            ],
            prices={"000001": 10.0, "600000": 20.0},
        )
        assert env.status == "ok"
        assert env.error is None
        data = env.data
        assert data is not None
        assert isinstance(data, list)
        assert len(data) == 3
        tiers = {item["tier"] for item in data}
        assert tiers == {"100k", "200k", "500k"}
        first = data[0]
        assert "feasible" in first
        assert "cash_utilization" in first
        assert "tracking_error" in first
        assert "unfillable_symbols" in first
        assert "reasons" in first

    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await portfolio_tools.portfolio_feasibility(
            app,
            weights={"000001": 1.0},
            as_of="2026-01-15",
            lot_info=[{"code": "000001", "lot_size": 100}],
            prices={"000001": 10.0},
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"


# ---------------------------------------------------------------------------
# portfolio_attribution
# ---------------------------------------------------------------------------


class TestPortfolioAttribution:
    async def test_happy_path(self) -> None:
        """权重历史 + 收益序列(≥30 观测)→ 归因分解。"""
        # estimate_covariance 要求 ≥30 个观测值(MIN_OBS_FOR_FULL_COVARIANCE)。
        returns_a = [0.01, -0.005, 0.008, 0.002, -0.003] * 8  # 40 obs
        returns_b = [0.005, 0.002, -0.008, 0.004, 0.001] * 8  # 40 obs
        weights_hist = [{"000001": 0.5, "600000": 0.5}] * 40
        app = _make_app()
        env = await portfolio_tools.portfolio_attribution(
            app,
            weights_history=weights_hist,
            returns_by_ticker={"000001": returns_a, "600000": returns_b},
        )
        assert env.status == "ok"
        assert env.error is None
        data = env.data
        assert data is not None
        assert "by_asset" in data
        assert "by_sleeve" in data
        assert "total_return" in data
        assert "total_risk" in data
        assert "max_drawdown" in data
        assert isinstance(data["by_asset"], list)

    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await portfolio_tools.portfolio_attribution(
            app,
            weights_history=[{"000001": 1.0}],
            returns_by_ticker={"000001": [0.01, 0.02]},
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_empty_weights_history_invalid_argument(self) -> None:
        app = _make_app()
        env = await portfolio_tools.portfolio_attribution(
            app,
            weights_history=[],
            returns_by_ticker={"000001": [0.01, 0.02] * 20},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
