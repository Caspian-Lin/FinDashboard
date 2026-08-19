"""""#141 端到端:报告聚合与导出(MCP 工具,需要 DB)。

链路:直接落库一条 completed ResearchRun(+ artifacts)与一条 BacktestRun →
report_run 聚合(metrics + artifacts)→ report_backtest 聚合(metrics +
equity_curve + fills)→ report_export 导出 CSV / Markdown 文件(真实文件
可读、中文不乱码、UTF-8 BOM)→ 不存在的 run_id / backtest_id → not_found。

同时验证:MCP 工具与 REST/领域共用同一张表(MCP session 能读到 db_session
提交的数据);导出目录尊重 FINBOARD_EXPORT_DIR 环境变量。
"""""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import reports as rp_tools
from finboard_persistence import (
    BacktestRunModel,
    ResearchRunArtifactModel,
    ResearchRunModel,
    session_factory,
)

pytestmark = pytest.mark.asyncio

_RUN_ID = "RR-e2e-report-141"


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


async def _seed(db_session: AsyncSession) -> int:
    """落库一条 completed ResearchRun(+ artifacts)与一条 BacktestRun,返回回测 id。"""
    run = ResearchRunModel(
        run_id=_RUN_ID,
            idempotency_key="e2e-report-141",
            strategy_id="strat-e2e-141",
            strategy_kind="ma_cross",
            status="completed",
            schema_version="1.0",
            manifest_checksum="abc123",
            manifest={},
            result={
                "strategy_return": 0.21,
                "benchmark_return": 0.08,
                "sharpe_ratio": 1.9,
                "备注": "e2e 中文指标",
            },
            requested_by="integration-test",
            created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
            completed_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
    )
    db_session.add(run)
    await db_session.flush()  # 先落 research_runs,保证 artifact 外键可解析
    db_session.add(
        ResearchRunArtifactModel(
            run_id=_RUN_ID,
            artifact_id=f"{_RUN_ID}:A:report",
            sequence=96,
            stage="report",
            trace_id="RRT-e2e-report-artifact",
            parent_trace_ids=[],
            payload={"report": {"strategy_return": 0.21}},
            checksum="c1",
            created_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
        )
    )
    backtest = BacktestRunModel(
        strategy="ma_cross",
        symbols=["510300.SH", "513100.SH"],
        start="2024-01-01",
        end="2024-06-30",
        capital=Decimal("100000"),
        adjust="qfq",
        params={},
        metrics={"total_return": 0.15, "sharpe_ratio": 1.2},
        equity_curve=[
            {"date": "2024-01-05", "equity": 100000.0, "benchmark": 99500.0},
            {"date": "2024-01-08", "equity": 101234.0},
        ],
        fills=[
            {
                "date": "2024-01-05",
                "symbol": "510300.SH",
                "side": "buy",
                "quantity": 100,
                "price": 3.85,
                "commission": 1.0,
            }
        ],
        summary="e2e 回测摘要:年化 15%",
        created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
    )
    db_session.add(backtest)
    await db_session.commit()
    await db_session.refresh(backtest)
    return backtest.id


async def test_report_mcp_aggregate_and_export(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
    app = _make_app(_engine)
    backtest_id = await _seed(db_session)

    # 1) report_run:聚合 result 指标 + 全部 artifacts(真实 DB)
    #    issue #206:默认 view=summary(聚合计数),artifacts 走 view=detail。
    run_env = await rp_tools.report_run(app, _RUN_ID)
    assert run_env.status == "ok", run_env.error
    assert run_env.data["run_id"] == _RUN_ID
    assert run_env.data["status"] == "completed"
    assert run_env.data["metrics"]["strategy_return"] == 0.21
    assert run_env.data["metrics"]["备注"] == "e2e 中文指标"
    assert run_env.data["artifact_count"] == 1
    assert run_env.data["view"] == "summary"

    run_detail = await rp_tools.report_run(app, _RUN_ID, view="detail")
    assert run_detail.status == "ok", run_detail.error
    assert run_detail.data["artifacts"][0]["stage"] == "report"

    # 2) report_backtest:聚合 metrics + equity_curve + fills
    bt_env = await rp_tools.report_backtest(app, backtest_id)
    assert bt_env.status == "ok", bt_env.error
    assert bt_env.data["run_id"] == backtest_id
    assert bt_env.data["metrics"]["total_return"] == 0.15
    assert len(bt_env.data["equity_curve"]) == 2
    assert len(bt_env.data["fills"]) == 1
    assert bt_env.data["fills"][0]["symbol"] == "510300.SH"

    # 3) report_export run → CSV:真实文件可读,UTF-8 BOM,中文不乱码
    csv_env = await rp_tools.report_export(
        app, "run", _RUN_ID, "csv"
    )
    assert csv_env.status == "ok", csv_env.error
    csv_path = Path(csv_env.data["path"])
    assert csv_path.is_file()  # noqa: ASYNC240
    assert csv_path.parent == tmp_path
    csv_text = csv_path.read_text(  # noqa: ASYNC240
        encoding="utf-8-sig"
    )
    assert csv_text.startswith("# metadata")
    assert f"run_id,{_RUN_ID}" in csv_text
    assert "strategy_return,0.21" in csv_text
    assert "备注" in csv_text
    assert "e2e 中文指标" in csv_text
    assert "artifact_id,sequence,stage,decision_id,trace_id" in csv_text

    # 4) report_export backtest → Markdown:表格 + 中文正常
    md_env = await rp_tools.report_export(
        app, "backtest", str(backtest_id), "markdown"
    )
    assert md_env.status == "ok", md_env.error
    md_path = Path(md_env.data["path"])
    assert md_path.is_file()  # noqa: ASYNC240
    md_text = md_path.read_text(  # noqa: ASYNC240
        encoding="utf-8"
    )
    assert md_text.startswith(f"# FinBoard backtest 报告({backtest_id})")
    assert "## equity_curve" in md_text
    assert "| 2024-01-05 | 100000.0 | 99500.0 |" in md_text
    assert "## fills" in md_text
    assert "e2e 回测摘要" in md_text

    # 5) 错误处理:不存在的 run_id / backtest_id → not_found;未知格式 → invalid_argument
    miss_env = await rp_tools.report_run(app, "RR-does-not-exist")
    assert miss_env.status == "error"
    assert miss_env.error is not None
    assert miss_env.error.kind == "not_found"
    miss_env = await rp_tools.report_export(
        app, "backtest", "999999", "csv"
    )
    assert miss_env.status == "error"
    assert miss_env.error is not None
    assert miss_env.error.kind == "not_found"
    bad_env = await rp_tools.report_export(
        app, "run", _RUN_ID, "xlsx"
    )
    assert bad_env.status == "error"
    assert bad_env.error is not None
    assert bad_env.error.kind == "invalid_argument"
