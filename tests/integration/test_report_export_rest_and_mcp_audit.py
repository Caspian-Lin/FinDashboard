"""#157 集成测试:Web 报告导出下载端点 + MCP 审计查询端点(需要 DB)。

链路:
1. 直接落库一条 completed ResearchRun(+ artifact)与一条 BacktestRun →
   ``GET /api/research/runs/{id}/report/export?format=csv|markdown`` /
   ``GET /api/backtest/history/{id}/report/export`` 返回文件下载
   (Content-Disposition 附件 + CSV 带 UTF-8 BOM + Markdown 表头);
   与 MCP ``finboard_report_export`` 复用同一套 ``finboard_mcp.reporting``
   渲染逻辑(不落盘);不存在 id → 404;非法 format → 422。
2. ``McpAuditRepository.append`` 写入 ``mcp_audit_events`` →
   ``GET /api/mcp/audit`` 返回记录(时间升序,支持 tool_name 过滤)。

路由只挂被测 router + ``get_db_session`` 覆盖,不起完整 lifespan。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.backtest import router as backtest_router
from finboard_api.routes.mcp_audit import router as mcp_audit_router
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_persistence import (
    BacktestRunModel,
    McpAuditRepository,
    ResearchRunArtifactModel,
    ResearchRunModel,
    session_factory,
)

pytestmark = pytest.mark.asyncio

_RUN_ID = "RR-e2e-rest-157"


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """复用 conftest 的建表 engine;模块结束清理本测试写入的审计行。"""
    yield _engine
    async with _engine.begin() as conn:
        await conn.execute(text("delete from mcp_audit_events"))


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """最小 app:只挂被测 router,``get_db_session`` 用同一 engine 的独立 session。"""
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(research_runs_router)
    app.include_router(backtest_router)
    app.include_router(mcp_audit_router)
    app.dependency_overrides[get_db_session] = _override

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def _seed(engine: AsyncEngine) -> int:
    """落库一条 completed ResearchRun(+ artifact)与一条 BacktestRun,返回回测 id。

    同一测试类多个用例各自调用,先清理旧 run(artifact 有 FK,先删子表)保证幂等。
    """
    async with session_factory(engine)() as session:
        await session.execute(
            text(
                "delete from research_run_artifacts where run_id = :rid"
            ).bindparams(rid=_RUN_ID)
        )
        await session.execute(
            text("delete from research_runs where run_id = :rid").bindparams(rid=_RUN_ID)
        )
        await session.commit()
    run = ResearchRunModel(
        run_id=_RUN_ID,
        idempotency_key="e2e-rest-157",
        strategy_id="strat-e2e-157",
        strategy_kind="ma_cross",
        status="completed",
        schema_version="1.0",
        manifest_checksum="abc157",
        manifest={},
        result={"strategy_return": 0.21, "sharpe_ratio": 1.9, "备注": "中文指标"},
        requested_by="integration-test",
        created_at=datetime(2026, 8, 14, 8, 30, tzinfo=UTC),
        completed_at=datetime(2026, 8, 14, 8, 35, tzinfo=UTC),
    )
    async with session_factory(engine)() as session:
        session.add(run)
        await session.flush()
        session.add(
            ResearchRunArtifactModel(
                run_id=_RUN_ID,
                artifact_id=f"{_RUN_ID}:A:report",
                sequence=1,
                stage="report",
                decision_id=None,
                trace_id=f"{_RUN_ID}:T:1",
                parent_trace_ids=[],
                payload={"strategy_return": 0.21},
                checksum="ck-1",
            )
        )
        backtest = BacktestRunModel(
            strategy="ma_cross",
            symbols=["600519"],
            start="2024-01-01",
            end="2024-06-30",
            capital=100000,
            adjust="qfq",
            params={},
            selection={},
            metrics={"total_return": 0.1},
            equity_curve=[
                {"date": "2024-01-02", "equity": 100000.0},
                {"date": "2024-01-03", "equity": 101000.0},
            ],
            fills=[],
            summary="e2e 回测摘要:总收益 10%",
        )
        session.add(backtest)
        await session.commit()
        return int(backtest.id)


class TestResearchRunReportExport:
    async def test_export_csv(self, engine, client) -> None:
        await _seed(engine)
        resp = await client.get(
            f"/api/research/runs/{_RUN_ID}/report/export", params={"format": "csv"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert "finboard_run_RR-e2e-rest-157.csv" in resp.headers["content-disposition"]
        body = resp.text
        assert body.startswith("\ufeff")  # UTF-8 BOM(Excel 兼容)
        assert "sharpe_ratio" in body
        assert "中文指标" in body
        assert "# metrics" in body

    async def test_export_markdown(self, engine, client) -> None:
        await _seed(engine)
        resp = await client.get(
            f"/api/research/runs/{_RUN_ID}/report/export",
            params={"format": "markdown"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert "attachment" in resp.headers["content-disposition"]
        assert ".md" in resp.headers["content-disposition"]
        body = resp.text
        assert body.startswith("# FinBoard run 报告")
        assert "## metrics" in body
        assert "| field | value |" in body

    async def test_export_not_found(self, client) -> None:
        resp = await client.get(
            "/api/research/runs/RR-not-exist/report/export", params={"format": "csv"}
        )
        assert resp.status_code == 404

    async def test_export_rejects_unknown_format(self, client) -> None:
        resp = await client.get(
            f"/api/research/runs/{_RUN_ID}/report/export", params={"format": "pdf"}
        )
        assert resp.status_code == 422


class TestBacktestReportExport:
    async def test_export_csv(self, engine, client) -> None:
        backtest_id = await _seed(engine)
        resp = await client.get(
            f"/api/backtest/history/{backtest_id}/report/export",
            params={"format": "csv"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "# equity_curve" in resp.text
        assert "100000.0" in resp.text

    async def test_export_markdown(self, engine, client) -> None:
        backtest_id = await _seed(engine)
        resp = await client.get(
            f"/api/backtest/history/{backtest_id}/report/export",
            params={"format": "markdown"},
        )
        assert resp.status_code == 200
        assert "# FinBoard backtest 报告" in resp.text

    async def test_export_not_found(self, client) -> None:
        resp = await client.get(
            "/api/backtest/history/999999999/report/export", params={"format": "csv"}
        )
        assert resp.status_code == 404


class TestMcpAuditEndpoint:
    async def test_list_recent_and_filter(self, engine, client) -> None:
        async with session_factory(engine)() as session:
            repo = McpAuditRepository(session)
            await repo.append(
                operation_id="OP-rest-1",
                tool_name="finboard.report.run",
                status="ok",
                latency_ms=5,
                error_kind=None,
                caller="agent:mcp",
                arguments_summary={"run_id": "RR-x"},
            )
            await repo.append(
                operation_id="OP-rest-2",
                tool_name="finboard.factor.catalog",
                status="ok",
                latency_ms=3,
                error_kind=None,
                caller="agent:mcp",
                arguments_summary={},
            )
            await session.commit()

        resp = await client.get("/api/mcp/audit", params={"limit": 10})
        assert resp.status_code == 200
        entries = resp.json()
        ids = [
            e["operation_id"]
            for e in entries
            if e["operation_id"].startswith("OP-rest")
        ]
        assert ids == ["OP-rest-1", "OP-rest-2"]  # 时间升序
        first = next(e for e in entries if e["operation_id"] == "OP-rest-1")
        assert first["tool_name"] == "finboard.report.run"
        assert first["arguments_summary"] == {"run_id": "RR-x"}
        assert first["caller"] == "agent:mcp"

        resp = await client.get(
            "/api/mcp/audit",
            params={"tool_name": "finboard.factor.catalog", "limit": 10},
        )
        assert resp.status_code == 200
        assert [e["operation_id"] for e in resp.json()] == ["OP-rest-2"]
