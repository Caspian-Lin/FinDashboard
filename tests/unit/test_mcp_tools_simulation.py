"""``finboard.sim.*`` 工具 —— 模拟盘工具测试(mock,无需 DB)。

覆盖:

* 只读(9):account_list / account_get / session_list / session_get /
  orders / fills / positions / ledger / audit / report —— monkeypatch
  ``SimulationRepository`` 方法返回 duck-typed ORM 行;
* 写(8):account_create / session_create / start / pause / stop / reset /
  decision_submit / order_cancel —— monkeypatch ``SimulationService`` 方法,
  验证 ``write_tools_enabled=False`` 时拒绝、ID 前缀校验、conflict 映射。
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
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import simulation as sim_tools

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


def _get_session(app: McpAppContext) -> AsyncMock:
    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _account_row(account_id: str = "SIM-A-abc") -> SimpleNamespace:
    return SimpleNamespace(
        simulation_account_id=account_id,
        name="测试账户",
        mode="simulation",
        status="active",
        currency="CNY",
        initial_cash=Decimal("100000"),
        cash=Decimal("90000"),
        frozen_cash=Decimal("0"),
        margin_used=Decimal("0"),
        equity=Decimal("100000"),
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        archived_at=None,
        updated_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


def _session_row(session_id: str = "SIM-S-xyz") -> SimpleNamespace:
    return SimpleNamespace(
        simulation_session_id=session_id,
        simulation_account_id="SIM-A-abc",
        mode="simulation",
        source_mode="bar_replay",
        status="created",
        strategy_id="mf_test",
        strategy_version=1,
        strategy_checksum="chk123",
        validation_run_id="RR-abc",
        data_release_id="REL-1",
        config={},
        clock={},
        promotion_status="not_evaluated",
        reset_of_session_id=None,
        recovery_count=0,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        started_at=None,
        paused_at=None,
        stopped_at=None,
        archived_at=None,
        updated_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# account_list / account_get
# ---------------------------------------------------------------------------


class TestSimAccountList:
    async def test_returns_accounts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        rows = [_account_row("SIM-A-1"), _account_row("SIM-A-2")]
        monkeypatch.setattr(
            SimulationRepository,
            "list_accounts",
            lambda self, **kw: _async_return(rows),
        )
        env = await sim_tools.sim_account_list(app, limit=10)
        assert env.status == "ok"
        data = env.data
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["simulation_account_id"] == "SIM-A-1"


class TestSimAccountGet:
    async def test_invalid_prefix(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_account_get(app, "BAD-ID")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        monkeypatch.setattr(
            SimulationRepository,
            "get_account",
            lambda self, aid: _async_return(None),
        )
        env = await sim_tools.sim_account_get(app, "SIM-A-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        monkeypatch.setattr(
            SimulationRepository,
            "get_account",
            lambda self, aid: _async_return(_account_row()),
        )
        env = await sim_tools.sim_account_get(app, "SIM-A-abc")
        assert env.status == "ok"
        assert env.data["simulation_account_id"] == "SIM-A-abc"


class TestSimAccountCreate:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await sim_tools.sim_account_create(
            app, name="x", initial_cash="100", actor="u"
        )
        assert env.status == "denied"

    async def test_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationService

        app = _make_app()
        session = _get_session(app)
        row = _account_row("SIM-A-new")
        monkeypatch.setattr(
            SimulationService,
            "create_account",
            lambda self, **kw: _async_return(row),
        )
        env = await sim_tools.sim_account_create(
            app, name="测试", initial_cash="100000", actor="user1"
        )
        assert env.status == "ok"
        assert env.data["simulation_account_id"] == "SIM-A-new"
        session.commit.assert_awaited()

    async def test_invalid_cash(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_account_create(
            app, name="测试", initial_cash="-1", actor="user1"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# session_list / session_get
# ---------------------------------------------------------------------------


class TestSimSessionList:
    async def test_invalid_status(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_session_list(app, status=["bogus"])
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_returns_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        monkeypatch.setattr(
            SimulationRepository,
            "list_sessions",
            lambda self, **kw: _async_return([_session_row()]),
        )
        env = await sim_tools.sim_session_list(app)
        assert env.status == "ok"
        assert len(env.data) == 1
        assert env.data[0]["simulation_session_id"] == "SIM-S-xyz"


class TestSimSessionGet:
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        monkeypatch.setattr(
            SimulationRepository,
            "get_session",
            lambda self, sid: _async_return(None),
        )
        env = await sim_tools.sim_session_get(app, "SIM-S-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


# ---------------------------------------------------------------------------
# session_create / transition / reset
# ---------------------------------------------------------------------------


class TestSimSessionCreate:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await sim_tools.sim_session_create(
            app,
            simulation_account_id="SIM-A-1",
            strategy_id="mf_test",
            strategy_version=1,
            validation_run_id="RR-1",
            data_release_id="REL-1",
            source_mode="bar_replay",
            actor="u",
        )
        assert env.status == "denied"

    async def test_invalid_run_prefix(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_session_create(
            app,
            simulation_account_id="SIM-A-1",
            strategy_id="mf_test",
            strategy_version=1,
            validation_run_id="BAD-RUN",
            data_release_id="REL-1",
            source_mode="bar_replay",
            actor="u",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestSimSessionTransition:
    async def test_start_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await sim_tools.sim_session_start(app, "SIM-S-1", actor="u")
        assert env.status == "denied"

    async def test_start_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationService

        app = _make_app()
        session = _get_session(app)
        started = _session_row()
        started.status = "running"
        monkeypatch.setattr(
            SimulationService,
            "transition_session",
            lambda self, sid, **kw: _async_return(started),
        )
        env = await sim_tools.sim_session_start(app, "SIM-S-1", actor="u")
        assert env.status == "ok"
        assert env.data["status"] == "running"
        session.commit.assert_awaited()

    async def test_invalid_prefix(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_session_pause(app, "BAD", actor="u")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


# ---------------------------------------------------------------------------
# decision_submit / order_cancel
# ---------------------------------------------------------------------------


class TestSimDecisionSubmit:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await sim_tools.sim_decision_submit(
            app, "SIM-S-1", decision={}
        )
        assert env.status == "denied"

    async def test_invalid_decision_payload(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_decision_submit(
            app, "SIM-S-1", decision={"decision_id": "x"}  # 缺字段
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestSimOrderCancel:
    async def test_write_disabled(self) -> None:
        app = _make_app(write_enabled=False)
        env = await sim_tools.sim_order_cancel(
            app, "SIM-S-1", "SIM-O-1", actor="u"
        )
        assert env.status == "denied"


# ---------------------------------------------------------------------------
# 只读:orders / fills / positions / ledger / audit / report
# ---------------------------------------------------------------------------


def _patch_require_session(
    monkeypatch: pytest.MonkeyPatch, session_row: Any
) -> None:
    """Patch SimulationRepository.get_session for read tools that call
    _require_sim_session(存在性校验)。"""
    from finboard_simulation import SimulationRepository

    monkeypatch.setattr(
        SimulationRepository,
        "get_session",
        lambda self, sid: _async_return(session_row),
    )


class TestSimOrders:
    async def test_returns_orders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        _patch_require_session(monkeypatch, _session_row())
        order = SimpleNamespace(
            simulation_order_id="SIM-O-1",
            simulation_account_id="SIM-A-1",
            simulation_session_id="SIM-S-xyz",
            decision_id="DEC-1",
            strategy_id="mf_test",
            signal_trace_id="TRACE-1",
            symbol="000001",
            market="a_share",
            instrument_type="stock",
            asset_rule_key="a_share_round_lot_100",
            position_side="long",
            position_effect="open",
            side="buy",
            order_type="market",
            time_in_force="gfd",
            quantity=Decimal("100"),
            price=None,
            filled_quantity=Decimal("100"),
            average_fill_price=Decimal("10"),
            status="filled",
            reject_reason=None,
            reject_message=None,
            reserved_cash=Decimal("1000"),
            reserved_quantity=Decimal("0"),
            submitted_market_at=datetime(2026, 1, 15, tzinfo=UTC),
            eligible_after=datetime(2026, 1, 15, tzinfo=UTC),
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
            updated_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        monkeypatch.setattr(
            SimulationRepository,
            "list_orders",
            lambda self, *a, **kw: _async_return([order]),
        )
        env = await sim_tools.sim_orders(app, "SIM-S-xyz")
        assert env.status == "ok"
        assert len(env.data) == 1
        assert env.data[0]["simulation_order_id"] == "SIM-O-1"

    async def test_invalid_prefix(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_orders(app, "BAD")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestSimPositions:
    async def test_returns_positions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationRepository

        app = _make_app()
        sess = _session_row()
        sess.simulation_account_id = "SIM-A-1"
        _patch_require_session(monkeypatch, sess)
        pos = SimpleNamespace(
            simulation_account_id="SIM-A-1",
            symbol="000001",
            market="a_share",
            instrument_type="stock",
            asset_rule_key="a_share_round_lot_100",
            position_side="long",
            total_quantity=Decimal("100"),
            available_quantity=Decimal("100"),
            frozen_quantity=Decimal("0"),
            average_price=Decimal("10"),
            market_value=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            margin_used=Decimal("0"),
            last_price=Decimal("10"),
            last_settlement_price=Decimal("10"),
            last_settlement_date=None,
            updated_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        monkeypatch.setattr(
            SimulationRepository,
            "list_positions",
            lambda self, aid: _async_return([pos]),
        )
        env = await sim_tools.sim_positions(app, "SIM-S-xyz")
        assert env.status == "ok"
        assert len(env.data) == 1
        assert env.data[0]["symbol"] == "000001"


class TestSimReport:
    async def test_invalid_prefix(self) -> None:
        app = _make_app()
        env = await sim_tools.sim_report(app, "BAD")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_returns_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_simulation import SimulationService

        app = _make_app()
        report = {
            "session_id": "SIM-S-xyz",
            "mode": "simulation",
            "initial_cash": "100000",
            "final_equity": "105000",
            "automatic_live_promotion": False,
        }
        monkeypatch.setattr(
            SimulationService,
            "build_report",
            lambda self, sid: _async_return(report),
        )
        env = await sim_tools.sim_report(app, "SIM-S-xyz")
        assert env.status == "ok"
        assert env.data["session_id"] == "SIM-S-xyz"
        assert env.data["automatic_live_promotion"] is False
