""""#139 端到端:模拟盘补全工具(archive / market_event / evaluate,需要 DB)。

链路(agent 视角的模拟盘完整生命周期):
finboard_sim_account_create → finboard_sim_session_create(绑定已发布策略 +
completed ResearchRun + 数据发布)→ finboard_sim_session_start →
finboard_sim_market_event(投 K 线)→ finboard_sim_decision_submit(结构化目标
仓位 → 生成订单,不直接创建订单)→ finboard_sim_market_event(下一根 K 线
撮合成交;同 source_event_id 重投 → duplicate=true)→
finboard_sim_session_stop → finboard_sim_session_evaluate(eligible,
automatic_live_promotion=false)→ finboard_sim_session_archive(stopped →
archived,账户同步归档)。

同时验证:MCP 工具与领域共用同一张表;状态机约束映射为 conflict(非 running
投 K 线 / 非 stopped 评估)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_backtest.strategy_spec import (
    build_strategy_template,
    strategy_spec_checksum,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import simulation as sim_tools
from finboard_persistence import (
    ResearchRunArtifactModel,
    ResearchRunModel,
    ResearchStrategySpecModel,
    session_factory,
)
from finboard_simulation import stable_checksum

pytestmark = pytest.mark.asyncio

_STRATEGY_ID = "sim_etf_rotation"
_STRATEGY_KIND = "etf_rotation"
_RUN_ID = f"RR-mcp-sim-e2e-{uuid4().hex[:8]}"
_RELEASE_ID = f"release-mcp-sim-e2e-{uuid4().hex[:8]}"
_RESEARCH_DECISION = f"research-decision-{uuid4().hex[:8]}"
_TRACE_ID = f"trace-mcp-sim-{uuid4().hex[:8]}"
_DECISION_ID = f"mcp-sim-decision-{uuid4().hex[:8]}"
_DAY1 = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
_DAY2 = datetime(2026, 1, 6, 9, 30, tzinfo=UTC)
_DAY3 = datetime(2026, 1, 7, 9, 30, tzinfo=UTC)


async def _seed_research(session: AsyncSession) -> None:
    """播种已发布策略 + completed ResearchRun + 信号 trace(领域前置,非 MCP)。"""
    spec = build_strategy_template(
        _STRATEGY_KIND,
        strategy_id=_STRATEGY_ID,
        dataset_release_ids=(_RELEASE_ID,),
    )
    checksum = strategy_spec_checksum(spec)
    session.add(
        ResearchStrategySpecModel(
            strategy_id=_STRATEGY_ID,
            version=1,
            schema_version="v1",
            name=spec.name,
            strategy_kind=_STRATEGY_KIND,
            status="published",
            change_type="create",
            checksum=checksum,
            payload=spec.canonical_payload(),
            validation_errors=[],
            published_at=datetime.now(UTC),
        )
    )
    manifest: dict[str, object] = {
        "strategy_spec_checksum": checksum,
        "strategy_spec": spec.canonical_payload(),
        "dataset_releases": [
            {
                "artifact_id": _RELEASE_ID,
                "version": "v1",
                "checksum": "release-checksum",
            }
        ],
    }
    session.add(
        ResearchRunModel(
            run_id=_RUN_ID,
            idempotency_key=f"idem-{_RUN_ID}",
            replay_of_run_id=None,
            strategy_id=_STRATEGY_ID,
            strategy_kind=_STRATEGY_KIND,
            status="completed",
            schema_version="v2",
            manifest_checksum=stable_checksum(manifest),
            manifest=manifest,
            result={
                "final_equity": "105000",
                "accounting_invariants_passed": True,
            },
            result_checksum="result-checksum",
            requested_by="researcher",
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()
    session.add(
        ResearchRunArtifactModel(
            run_id=_RUN_ID,
            artifact_id=f"{_RUN_ID}:signals",
            decision_id=_RESEARCH_DECISION,
            sequence=1,
            stage="signals",
            trace_id=_TRACE_ID,
            parent_trace_ids=[],
            payload={"signals": [{"symbol": _STRATEGY_ID}]},
            checksum="signal-checksum",
        )
    )
    await session.flush()


def _make_app(_engine: AsyncEngine) -> McpAppContext:
    from finboard_app.config import Settings

    return McpAppContext(
        settings=Settings(),
        session_maker=session_factory(_engine),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=_engine,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


def _bar(source_event_id: str, timestamp: datetime) -> dict[str, Any]:
    return {
        "source_event_id": source_event_id,
        "symbol": "510300.SH",
        "market": "a_share",
        "period": "1d",
        "timestamp": timestamp.isoformat(),
        "open": "10.0",
        "high": "10.2",
        "low": "9.8",
        "close": "10.1",
        "volume": "10000",
        "amount": "101000",
        "actor": "market-data",
    }


def _decision() -> dict[str, Any]:
    return {
        "decision_id": _DECISION_ID,
        "source_run_id": _RUN_ID,
        "source_decision_id": _RESEARCH_DECISION,
        "actor": "mcp-agent",
        "targets": [
            {
                "symbol": "510300.SH",
                "market": "a_share",
                "instrument_type": "etf",
                "asset_rule_key": "equity_etf",
                "position_side": "long",
                "target_quantity": "1000",
                "signal_trace_id": _TRACE_ID,
                "reason": "MCP e2e 撮合目标",
            }
        ],
    }


async def test_simulation_mcp_full_lifecycle(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    # 1) 播种研究前置(已发布策略 + completed ResearchRun + 信号 trace)
    await _seed_research(db_session)
    await db_session.commit()

    app = _make_app(_engine)

    # 2) 创建模拟账户
    acc = await sim_tools.sim_account_create(
        app, name="MCP e2e", initial_cash="100000", actor="mcp-agent"
    )
    assert acc.status == "ok", acc.error
    account_id = acc.data["simulation_account_id"]
    assert account_id.startswith("SIM-A-")

    # 3) 创建模拟会话(绑定已发布策略 + completed ResearchRun + 数据发布)
    sess = await sim_tools.sim_session_create(
        app,
        simulation_account_id=account_id,
        strategy_id=_STRATEGY_ID,
        strategy_version=1,
        validation_run_id=_RUN_ID,
        data_release_id=_RELEASE_ID,
        source_mode="historical_replay",
        actor="mcp-agent",
    )
    assert sess.status == "ok", sess.error
    session_id = sess.data["simulation_session_id"]
    assert sess.data["status"] == "created"

    # 4) 启动会话
    started = await sim_tools.sim_session_start(app, session_id, actor="mcp-agent")
    assert started.status == "ok", started.error
    assert started.data["status"] == "running"

    # 5) 投第一根 K 线(无订单,不产生成交)
    bar1 = _bar("mcp-e2e-bar-1", _DAY1)
    first = await sim_tools.sim_market_event(app, session_id, bar=bar1)
    assert first.status == "ok", first.error
    assert first.data["duplicate"] is False
    assert first.data["fill_ids"] == []
    assert first.data["clock_at"] == _DAY1.isoformat().replace("+00:00", "Z")

    # 6) 提交结构化目标仓位决策 → 生成订单(agent 不直接创建订单)
    dec = await sim_tools.sim_decision_submit(app, session_id, decision=_decision())
    assert dec.status == "ok", dec.error
    assert dec.data["duplicate"] is False
    assert len(dec.data["orders"]) == 1
    assert dec.data["orders"][0]["status"] == "acknowledged"

    # 7) 同 source_event_id 重投同一根 K 线 → 幂等(duplicate=true)
    dup = await sim_tools.sim_market_event(app, session_id, bar=bar1)
    assert dup.status == "ok", dup.error
    assert dup.data["duplicate"] is True
    assert dup.data["fill_ids"] == []

    # 8) 投第二根 K 线(下一交易日)→ 撮合成交 + 权益更新
    second = await sim_tools.sim_market_event(
        app, session_id, bar=_bar("mcp-e2e-bar-2", _DAY2)
    )
    assert second.status == "ok", second.error
    assert second.data["duplicate"] is False
    assert len(second.data["fill_ids"]) == 1
    assert Decimal(second.data["equity"]) > 0

    # 9) 持仓/成交可经只读工具查询(同一张表)
    pos = await sim_tools.sim_positions(app, session_id)
    assert pos.status == "ok", pos.error
    assert len(pos.data) == 1
    assert pos.data[0]["symbol"] == "510300.SH"
    assert Decimal(pos.data[0]["total_quantity"]) == Decimal("1000")
    fills = await sim_tools.sim_fills(app, session_id)
    assert fills.status == "ok", fills.error
    assert len(fills.data) == 1

    # 10) 停止会话
    stopped = await sim_tools.sim_session_stop(app, session_id, actor="mcp-agent")
    assert stopped.status == "ok", stopped.error
    assert stopped.data["status"] == "stopped"

    # 11) 晋级评估:2 个交易日 ≥ 2 → eligible,automatic_live_promotion 恒 false
    ev = await sim_tools.sim_session_evaluate(app, session_id, actor="mcp-agent")
    assert ev.status == "ok", ev.error
    assert ev.data["promotion_status"] == "eligible"
    assert ev.data["trading_days"] == 2
    assert ev.data["minimum_trading_days"] == 2
    assert ev.data["automatic_live_promotion"] is False

    # 12) 非 running 会话投 K 线 → conflict(状态机约束)
    bar3 = _bar("mcp-e2e-bar-3", _DAY3)
    rejected_bar = await sim_tools.sim_market_event(app, session_id, bar=bar3)
    assert rejected_bar.status == "error"
    assert rejected_bar.error is not None
    assert rejected_bar.error.kind == "conflict"

    # 13) 归档(stopped → archived,账户同步归档)
    arch = await sim_tools.sim_session_archive(app, session_id, actor="mcp-agent")
    assert arch.status == "ok", arch.error
    assert arch.data["status"] == "archived"
    acc_after = await sim_tools.sim_account_get(app, account_id)
    assert acc_after.status == "ok", acc_after.error
    assert acc_after.data["status"] == "archived"

    # 14) 归档后评估 → conflict(仅 stopped 可评估)
    ev2 = await sim_tools.sim_session_evaluate(app, session_id, actor="mcp-agent")
    assert ev2.status == "error"
    assert ev2.error is not None
    assert ev2.error.kind == "conflict"
