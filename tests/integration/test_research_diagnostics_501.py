"""SQL投影、单决策护栏及确定性区间报告。"""

from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from finboard_app.research_diagnostics import decision_projection, interval_report
from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel
from tests.integration.test_research_workspace_500 import add_evidence


async def test_page_filters_large_payload_and_reports_total(db_session):
    await add_evidence(db_session)
    page = await decision_projection(db_session, run_id="RR-bounded-explain", stage="features",
                                     fields=["symbol", "value"], limit=2)
    assert page["total"] == 1001
    assert page["next_offset"] == 2
    selected = await decision_projection(db_session, run_id="RR-bounded-explain", stage="features", symbol="A")
    assert selected["total"] == 1
    assert "ignored_large" not in str(selected)
    assert selected["items"][0]["trace_id"] == "trace-features"


async def test_single_decision_guard_before_materializing_rows(db_session):
    await add_evidence(db_session)
    with patch("finboard_app.research_diagnostics.MAX_BYTES", 1):
        statements = []
        engine = db_session.bind.sync_engine
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(ValueError, match="payload_too_large"):
                await decision_projection(db_session, run_id="RR-bounded-explain", stage="features",
                    decision_id="RR-bounded-explain:D:00000001", fields=["value"], limit=1)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert not any(s.lstrip().startswith("SELECT a.decision_id") for s in statements)


async def test_year_and_fill_boundary_report(db_session):
    run = ResearchRunModel(run_id="RR-period", idempotency_key="period", strategy_id="fixture",
        strategy_kind="multi_factor", status="completed", schema_version="v2", manifest_checksum="a"*64,
        manifest={"initial_capital": "100000"}, requested_by="test",
        result={"equity_curve": [{"trade_date": "2024-12-31", "equity": "100000"},
                                 {"trade_date": "2025-01-02", "equity": "110000"}]})
    db_session.add(run)
    await db_session.flush()
    db_session.add(ResearchRunArtifactModel(artifact_id="RR-period-fill", run_id=run.run_id,
        decision_id="RR-period-D1", sequence=1, stage="fills", trace_id="fill-trace", parent_trace_ids=[],
        checksum="b"*64, payload={"business_date": "2024-12-31", "fills": [
            {"quantity": "100", "price": "10", "commission": "3", "tax": "0", "slippage": "1",
             "filled_at": "2025-01-02T09:30:00+08:00"}]}))
    await db_session.commit()
    report = await interval_report(db_session, run_id=run.run_id, start="2024-12-31", end="2025-01-02")
    assert report["years"][0]["fills"]["fill_count"] == 0
    assert report["years"][1]["fills"]["fill_count"] == 1
    assert report["years"][1]["net_return"] == pytest.approx(0.1)
    assert report["period"]["shortfall"]["ratio"] is None
    assert "10.00%" in report["markdown"]
    from finboard_api.routes.research_diagnostics import call as rest_call
    from finboard_mcp.tools.diagnostics import call as mcp_call
    from tests.unit.test_mcp_tools_reports import _make_app

    args = {"run_id": run.run_id, "start": "2024-12-31", "end": "2025-01-02"}
    rest = await rest_call(db_session, "interval", args)
    mcp = await mcp_call(_make_app(async_sessionmaker(db_session.bind, expire_on_commit=False)), "interval", args)
    assert mcp.status == "ok"
    assert mcp.data == rest
