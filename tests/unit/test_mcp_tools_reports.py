"""``finboard.report.*`` 工具 —— 报告聚合与导出测试(mock session,无需 DB)。

覆盖:

* 聚合:report_run(result 指标 + 全部 artifacts)/ report_backtest(metrics +
  equity_curve + fills + summary)字段映射完整、数据完整;
* 导出:CSV / Markdown 格式正确、文件可写可读、中文不乱码(UTF-8 BOM)、
  导出目录尊重 FINBOARD_EXPORT_DIR;
* 错误处理:不存在的 run_id / backtest_id → not_found,未知 kind / format /
  非整数回测 id → invalid_argument;
* 只读工具在 mcp_readonly_only 下仍可用(不设写守卫)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import reports as rp_tools
from finboard_persistence import (
    BacktestRunModel,
    ResearchRunArtifactModel,
    ResearchRunModel,
)

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_RUN_ID = "RR-abcdef0123456789abcdef"


def _run_row(
    *,
    run_id: str = _RUN_ID,
    status: str = "completed",
    result: dict[str, Any] | None = None,
) -> ResearchRunModel:
    return ResearchRunModel(
        run_id=run_id,
        idempotency_key=f"ik-{run_id}",
        strategy_id="strat-demo-001",
        strategy_kind="ma_cross",
        status=status,
        schema_version="1.0",
        manifest_checksum="abc123",
        manifest={"ok": True},
        result=result,
        requested_by="unit-test",
        created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
        started_at=datetime(2026, 1, 15, 8, 31, tzinfo=UTC),
        completed_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
    )


def _artifact(
    *,
    artifact_id: str = f"{_RUN_ID}:A:report",
    sequence: int = 96,
    stage: str = "report",
    payload: dict[str, object] | None = None,
) -> ResearchRunArtifactModel:
    return ResearchRunArtifactModel(
        run_id=_RUN_ID,
        artifact_id=artifact_id,
        sequence=sequence,
        stage=stage,
        trace_id=f"RRT-{artifact_id[:24]}",
        parent_trace_ids=[],
        payload=payload or {"report": {"strategy_return": 0.123}},
        checksum="c1",
        created_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
    )


def _backtest_row(*, run_id: int = 1) -> BacktestRunModel:
    return BacktestRunModel(
        id=run_id,
        strategy="ma_cross",
        symbols=["000001.SZ", "510300.SH"],
        start="2024-01-01",
        end="2024-12-31",
        capital=Decimal("100000"),
        adjust="qfq",
        params={},
        metrics={"total_return": 0.1234, "sharpe_ratio": 1.5},
        equity_curve=[
            {"date": "2024-01-05", "equity": 100000.0, "benchmark": 99000.0},
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
        summary="均线交叉策略回测,年化收益 12.3%",
        created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
    )


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


def _patch_run_repos(
    monkeypatch: Any,
    row: ResearchRunModel | None,
    artifacts: list[ResearchRunArtifactModel] | None = None,
) -> None:
    from finboard_persistence import ResearchRunRepository

    monkeypatch.setattr(
        ResearchRunRepository, "get", lambda self, rid: _async_return(row)
    )
    monkeypatch.setattr(
        ResearchRunRepository,
        "list_artifacts",
        lambda self, rid: _async_return(artifacts or []),
    )


def _patch_backtest_repos(
    monkeypatch: Any,
    row: BacktestRunModel | None,
) -> None:
    from finboard_persistence import BacktestRunRepository

    monkeypatch.setattr(
        BacktestRunRepository, "get", lambda self, rid: _async_return(row)
    )


# ---------------------------------------------------------------------------
# finboard.report.run(只读)
# ---------------------------------------------------------------------------


class TestReportRun:
    async def test_aggregates_metrics_and_artifacts(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app()
        result = {
            "strategy_return": 0.123,
            "benchmark_return": 0.05,
            "excess_return": 0.073,
            "sharpe_ratio": 1.8,
            "max_drawdown": -0.12,
            "final_equity": "112345.67",
            "constraint_impact": {"total_turnover_impact": 0.002},
            "decision_count": 12,
            "order_count": 24,
            "fill_count": 24,
            "accounting_invariants_passed": True,
        }
        row = _run_row(result=result)
        artifacts = [
            _artifact(),
            _artifact(
                artifact_id=f"{_RUN_ID}:A:00000000:equity",
                sequence=1,
                stage="equity",
                payload={"equity": 101000.0},
            ),
        ]
        _patch_run_repos(monkeypatch, row, artifacts)
        env = await rp_tools.report_run(app, _RUN_ID)
        assert env.status == "ok"
        data = env.data
        assert data["run_id"] == _RUN_ID
        assert data["status"] == "completed"
        assert data["strategy_id"] == "strat-demo-001"
        assert data["strategy_kind"] == "ma_cross"
        assert data["metrics"]["strategy_return"] == 0.123
        assert data["metrics"]["constraint_impact"]["total_turnover_impact"] == 0.002
        assert data["artifact_count"] == 2
        assert data["artifacts"][0]["stage"] == "report"
        assert data["artifacts"][0]["payload"]["report"]["strategy_return"] == 0.123
        assert data["artifacts"][1]["stage"] == "equity"
        assert data["created_at"] == "2026-01-15T08:30:00+00:00"
        assert data["completed_at"] == "2026-01-15T08:35:00+00:00"

    async def test_run_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_run_repos(monkeypatch, None)
        env = await rp_tools.report_run(app, "RR-nope")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_read_allowed_in_readonly_mode(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app(write_enabled=False)
        _patch_run_repos(monkeypatch, _run_row(result={"strategy_return": 0.1}))
        env = await rp_tools.report_run(app, _RUN_ID)
        assert env.status == "ok"
        assert env.data["metrics"]["strategy_return"] == 0.1


# ---------------------------------------------------------------------------
# finboard.report.backtest(只读)
# ---------------------------------------------------------------------------


class TestReportBacktest:
    async def test_aggregates_backtest_fields(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_backtest(app, 1)
        assert env.status == "ok"
        data = env.data
        assert data["run_id"] == 1
        assert data["strategy"] == "ma_cross"
        assert data["symbols"] == ["000001.SZ", "510300.SH"]
        assert data["start"] == "2024-01-01"
        assert data["end"] == "2024-12-31"
        assert data["capital"] == "100000"
        assert data["metrics"]["sharpe_ratio"] == 1.5
        assert len(data["equity_curve"]) == 2
        assert data["equity_curve"][0] == {
            "date": "2024-01-05", "equity": 100000.0, "benchmark": 99000.0
        }
        assert len(data["fills"]) == 1
        assert data["fills"][0]["symbol"] == "510300.SH"
        assert "均线交叉" in data["summary"]

    async def test_backtest_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_backtest_repos(monkeypatch, None)
        env = await rp_tools.report_backtest(app, 999)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_read_allowed_in_readonly_mode(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app(write_enabled=False)
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_backtest(app, 1)
        assert env.status == "ok"


# ---------------------------------------------------------------------------
# finboard.report.export(只读)
# ---------------------------------------------------------------------------


class TestReportExport:
    async def test_export_run_csv(self, monkeypatch: Any, tmp_path: Any) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        result = {
            "strategy_return": 0.123,
            "策略说明": "中文指标",
            "constraint_impact": {"total_turnover_impact": 0.002},
        }
        _patch_run_repos(monkeypatch, _run_row(result=result), [_artifact()])
        env = await rp_tools.report_export(
            app, "run", _RUN_ID, "csv"
        )
        assert env.status == "ok"
        data = env.data
        assert data["kind"] == "run"
        assert data["format"] == "csv"
        assert data["id"] == _RUN_ID
        path = Path(data["path"])
        assert path.is_file()  # noqa: ASYNC240
        assert path.parent == tmp_path
        assert data["size_bytes"] == path.stat().st_size  # noqa: ASYNC240
        text = path.read_text(encoding="utf-8-sig")  # noqa: ASYNC240
        assert text.startswith("# metadata")
        assert f"run_id,{_RUN_ID}" in text
        assert "status,completed" in text
        assert "# metrics" in text
        assert "strategy_return,0.123" in text
        assert "constraint_impact.total_turnover_impact,0.002" in text
        assert "# artifacts" in text
        assert "artifact_id,sequence,stage,decision_id,trace_id" in text
        assert "策略说明" in text
        assert "中文指标" in text

    async def test_export_run_markdown(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        _patch_run_repos(
            monkeypatch, _run_row(result={"strategy_return": 0.123}), [_artifact()]
        )
        env = await rp_tools.report_export(
            app, "run", _RUN_ID, "markdown"
        )
        assert env.status == "ok"
        text = Path(env.data["path"]).read_text(  # noqa: ASYNC240
            encoding="utf-8"
        )
        assert text.startswith(f"# FinBoard run 报告({_RUN_ID})")
        assert "## metadata" in text
        assert "| field | value |" in text
        assert f"| run_id | {_RUN_ID} |" in text
        assert "## metrics" in text
        assert "| strategy_return | 0.123 |" in text
        assert "## artifacts" in text
        assert "| artifact_id | sequence | stage | decision_id | trace_id |" in text

    async def test_export_backtest_csv(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(app, "backtest", "1", "csv")
        assert env.status == "ok"
        text = Path(env.data["path"]).read_text(  # noqa: ASYNC240
            encoding="utf-8-sig"
        )
        assert "# metadata" in text
        assert "# metrics" in text
        assert "sharpe_ratio,1.5" in text
        assert "# equity_curve" in text
        assert "date,equity,benchmark" in text
        assert "2024-01-05,100000.0,99000.0" in text
        assert "# fills" in text
        assert "date,symbol,side,quantity,price,commission" in text
        assert "510300.SH" in text
        assert "均线交叉" in text

    async def test_export_backtest_markdown(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(app, "backtest", "1", "markdown")
        assert env.status == "ok"
        text = Path(env.data["path"]).read_text(  # noqa: ASYNC240
            encoding="utf-8"
        )
        assert text.startswith("# FinBoard backtest 报告(1)")
        assert "## equity_curve" in text
        assert "| date | equity | benchmark |" in text
        assert "| 2024-01-05 | 100000.0 | 99000.0 |" in text
        assert "## fills" in text
        assert "| 510300.SH | buy | 100 | 3.85 | 1.0 |" in text

    async def test_export_unknown_kind(self, monkeypatch: Any) -> None:
        app = _make_app()
        env = await rp_tools.report_export(app, "pdf", "1", "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_export_unknown_format(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(app, "backtest", "1", "json")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_export_run_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_run_repos(monkeypatch, None)
        env = await rp_tools.report_export(app, "run", "RR-nope", "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_export_backtest_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_backtest_repos(monkeypatch, None)
        env = await rp_tools.report_export(app, "backtest", "999", "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_export_backtest_id_not_integer(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app()
        env = await rp_tools.report_export(app, "backtest", "abc", "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_export_read_allowed_in_readonly_mode(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app(write_enabled=False)
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(app, "backtest", "1", "csv")
        assert env.status == "ok"
        assert Path(env.data["path"]).is_file()  # noqa: ASYNC240


# ---------------------------------------------------------------------------
# reporting 渲染层(直接单测)
# ---------------------------------------------------------------------------


class TestRenderFunctions:
    def test_render_csv_quotes_commas_and_has_bom(self) -> None:
        from finboard_mcp import reporting

        table = reporting._Table(
            title="t",
            columns=("a", "b"),
            rows=(("x,y", "z"), ("中文", "值")),
        )
        text = reporting.render_csv([table])
        assert text.startswith("\ufeff")
        assert text.startswith("\ufeff# t")
        assert "\"x,y\",z" in text
        assert "中文,值" in text

    def test_render_markdown_escapes_pipes(self) -> None:
        from finboard_mcp import reporting

        table = reporting._Table(
            title="t",
            columns=("a", "b"),
            rows=(("x|y", "z"),),
        )
        text = reporting.render_markdown("标题", [table])
        assert text.startswith("# 标题")
        assert "## t" in text
        assert r"| x\|y | z |" in text

    def test_export_dir_respects_env(self, monkeypatch: Any, tmp_path: Any) -> None:
        from finboard_mcp import reporting

        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        assert reporting.export_dir() == tmp_path
        monkeypatch.delenv("FINBOARD_EXPORT_DIR")
        default = reporting.export_dir()
        assert str(default).endswith("finboard_exports")

    def test_export_report_rejects_unknown(self, tmp_path: Any) -> None:
        from finboard_mcp import reporting

        report = {"run_id": "RR-x"}
        try:
            reporting.export_report("sim", report, "csv", directory=tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝未知 kind")
        try:
            reporting.export_report("run", report, "xlsx", directory=tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝未知 format")

