"""大 run 报告载荷护栏 + 按决策下钻 + 事件循环防毒化测试(issue #458,mock session)。

背景:RR-f593d9bfb5f4a0e2f3576ce2(75 期决策、976 artifacts、每期 features
含 5000+ 标的)诊断时四个 MCP 工具连环超时 —— ``report_run(view=detail)`` /
``report_export`` 把全部 artifact payload 同步聚合 + JSON 化,GB 级体量远超
客户端超时,且同步 CPU 段跑在事件循环上把后续请求全部拖死。

覆盖:

* detail 载荷护栏:估计超限 → 具名 ``payload_too_large``(不静默截断),
  错误信息含规模与替代路径;未超限 / summary 视图不受影响;
* ``decision_id`` 下钻:只返回该决策 artifacts、豁免护栏、无匹配返回
  ``not_found``、summary 传 ``decision_id`` 拒绝(invalid_argument);
* 事件循环存活:detail 聚合停在线程池内时,轻量工具调用短超时内返回;
* ``report_export``:kind=run 同护栏 + ``decision_id`` 过滤导出;
  kind=backtest 行为零变化(backtest 不接受 decision_id);
* 回归:``aggregate_run_report(view=summary)`` 输出与改动前逐字段相等;
  detail 未下钻不新增键(旧消费方零漂移);``estimate_run_detail_bytes``
  口径;``factor_series_get`` detail 同款护栏。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp import reporting
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import factor_series as fs_tools
from finboard_mcp.tools import reports as rp_tools
from finboard_mcp.tools import strategies as strategy_tools
from finboard_persistence import (
    BacktestRunModel,
    BacktestRunRepository,
    FactorSeriesRecord,
    FactorSeriesRepository,
    ResearchRunArtifactModel,
    ResearchRunArtifactSummary,
    ResearchRunModel,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
)

# ---------------------------------------------------------------------------
# fixtures / helpers(test_mcp_tools_reports.py 同款 mock 形态)
# ---------------------------------------------------------------------------

_RUN_ID = "RR-458aabbccddeeff00112233"
_COMMIT = "c" * 40


async def _async_return(value: object) -> object:
    return value


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
    artifact_id: str,
    sequence: int,
    stage: str,
    decision_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ResearchRunArtifactModel:
    return ResearchRunArtifactModel(
        run_id=_RUN_ID,
        artifact_id=artifact_id,
        decision_id=decision_id,
        sequence=sequence,
        stage=stage,
        trace_id=f"RRT-{sequence:04d}",
        parent_trace_ids=[],
        payload=payload if payload is not None else {"report": {"ok": True}},
        checksum="c1",
        created_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
    )


def _drilldown_artifacts() -> list[ResearchRunArtifactModel]:
    """两个决策 + 一个 run 级 artifact(decision_id=null)的小型 run。"""
    return [
        _artifact(
            artifact_id=f"{_RUN_ID}:A:report",
            sequence=96,
            stage="report",
            decision_id=None,
            payload={"report": {"strategy_return": 0.5}},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:2026-01-05:universe",
            sequence=1,
            stage="universe",
            decision_id="2026-01-05",
            payload={
                "candidates": [
                    {"symbol": "000001.SZ", "included": True, "reasons": []},
                ]
            },
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
            sequence=2,
            stage="features",
            decision_id="2026-01-05",
            payload={"features": {"000001.SZ": {"momentum": 0.1}}},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:2026-02-02:features",
            sequence=3,
            stage="features",
            decision_id="2026-02-02",
            payload={"features": {"600000.SH": {"momentum": 0.2}}},
        ),
    ]


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
        summary="均线交叉策略回测",
        created_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
    )


def _spec_row(strategy_id: str = "mf_test", version: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id=strategy_id,
        version=version,
        schema_version="v1",
        name="测试策略",
        strategy_kind="multi_factor",
        status="draft",
        change_type="create",
        checksum="chk123",
        payload={
            "schema_version": "v1",
            "strategy_id": strategy_id,
            "name": "测试策略",
            "strategy_kind": "multi_factor",
        },
        validation_errors=[],
        parent_version=None,
        rollback_of_version=None,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        published_at=None,
    )


def _passthrough_row(row: Any) -> dict[str, Any]:
    """绕过 ``_version_to_dict`` 的 pydantic 校验,直接拷字段。"""
    return {
        "strategy_id": row.strategy_id,
        "version": row.version,
        "status": row.status,
        "checksum": row.checksum,
        "spec": dict(row.payload),
        "created_at": row.created_at,
    }


def _make_app() -> McpAppContext:
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


def _patch_run_repos(
    monkeypatch: Any,
    row: ResearchRunModel | None,
    artifacts: list[ResearchRunArtifactModel] | None = None,
) -> None:
    monkeypatch.setattr(
        ResearchRunRepository,
        "get",
        lambda self, rid: _async_return(row),
    )
    monkeypatch.setattr(
        ResearchRunRepository,
        "list_artifacts",
        lambda self, rid: _async_return(artifacts or []),
    )
    # issue #478:summary 视图改走数据库侧聚合;单元层用 Python 参考实现
    # 构造等价计数(两边语义一致性由 #478 集成测试与真实 run 复测保证)。
    aggregate = reporting.summarize_run_artifacts(artifacts or [])
    summary = ResearchRunArtifactSummary(
        artifact_count=len(artifacts or []),
        universe_total=aggregate["universe"]["total"],
        universe_included=aggregate["universe"]["included"],
        universe_excluded_by_reason=dict(aggregate["universe"]["excluded_by_reason"]),
        fills_total=aggregate["fills"]["total"],
        fills_by_decision=dict(aggregate["fills"]["by_decision"]),
    )
    monkeypatch.setattr(
        ResearchRunRepository,
        "summarize_artifacts",
        lambda self, rid: _async_return(summary),
    )


def _patch_backtest_repos(monkeypatch: Any, row: BacktestRunModel | None) -> None:
    monkeypatch.setattr(
        BacktestRunRepository,
        "get",
        lambda self, rid: _async_return(row),
    )


# ---------------------------------------------------------------------------
# detail 载荷护栏
# ---------------------------------------------------------------------------


class TestDetailPayloadGuard:
    async def test_detail_over_limit_named_error(self, monkeypatch: Any) -> None:
        """估计超限 → 具名 payload_too_large,附规模与替代路径,不静默截断。"""
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 128)
        app = _make_app()
        artifacts = [
            _artifact(
                artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
                sequence=1,
                stage="features",
                decision_id="2026-01-05",
                payload={"blob": "x" * 200},
            ),
        ]
        _patch_run_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_run(app, _RUN_ID, view="detail")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert env.error.retryable is False
        message = env.error.message
        assert "不静默截断" in message
        assert "MB" in message  # 实际规模
        assert "summary" in message  # 替代路径 1
        assert "decision_id" in message  # 替代路径 2

    async def test_detail_under_limit_unchanged(self, monkeypatch: Any) -> None:
        """默认 64MB 上限下小 run detail 行为不变(护栏不误伤)。"""
        app = _make_app()
        artifacts = [
            _artifact(artifact_id=f"{_RUN_ID}:A:report", sequence=1, stage="report"),
        ]
        _patch_run_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_run(app, _RUN_ID, view="detail")
        assert env.status == "ok", env.error
        assert env.data["artifacts"][0]["stage"] == "report"
        assert "payload" in env.data["artifacts"][0]

    async def test_summary_view_not_guarded(self, monkeypatch: Any) -> None:
        """护栏只锁 detail;summary 聚合计数即便 payload 巨大也正常返回。"""
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        artifacts = [
            _artifact(
                artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
                sequence=1,
                stage="features",
                decision_id="2026-01-05",
                payload={"blob": "x" * 200},
            ),
        ]
        _patch_run_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_run(app, _RUN_ID, view="summary")
        assert env.status == "ok", env.error
        assert "artifacts" not in env.data


# ---------------------------------------------------------------------------
# decision_id 按决策下钻
# ---------------------------------------------------------------------------


class TestDecisionIdDrilldown:
    async def test_drilldown_returns_only_matching_artifacts(
        self, monkeypatch: Any
    ) -> None:
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())
        env = await rp_tools.report_run(
            app, _RUN_ID, view="detail", decision_id="2026-01-05"
        )
        assert env.status == "ok", env.error
        data = env.data
        assert data["decision_id"] == "2026-01-05"
        assert data["filtered_artifact_count"] == 2
        assert data["artifact_count"] == 4  # 保持 run 全量计数
        stages = [item["stage"] for item in data["artifacts"]]
        assert stages == ["universe", "features"]
        assert all(
            item["decision_id"] == "2026-01-05" for item in data["artifacts"]
        )
        # run 级 artifact(report,decision_id=null)不参与下钻匹配
        assert "report" not in stages
        # 字段齐全:payload 原样返回
        assert (
            data["artifacts"][0]["payload"]["candidates"][0]["included"] is True
        )

    async def test_drilldown_bypasses_guard(self, monkeypatch: Any) -> None:
        """下钻路径豁免护栏(单决策 artifacts 有界),超限 run 也能下钻。"""
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())
        env = await rp_tools.report_run(
            app, _RUN_ID, view="detail", decision_id="2026-02-02"
        )
        assert env.status == "ok", env.error
        assert env.data["filtered_artifact_count"] == 1

    async def test_drilldown_no_match_not_found(self, monkeypatch: Any) -> None:
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())
        env = await rp_tools.report_run(
            app, _RUN_ID, view="detail", decision_id="2026-03-03"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
        assert "2026-03-03" in env.error.message

    async def test_summary_with_decision_id_rejected(self, monkeypatch: Any) -> None:
        """summary 传 decision_id → invalid_argument(不静默忽略)。"""
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())
        env = await rp_tools.report_run(
            app, _RUN_ID, view="summary", decision_id="2026-01-05"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "detail" in env.error.message


# ---------------------------------------------------------------------------
# 事件循环防毒化
# ---------------------------------------------------------------------------


class TestEventLoopNotPoisoned:
    async def test_light_call_not_blocked_while_detail_aggregates(
        self, monkeypatch: Any
    ) -> None:
        """detail 聚合停在线程池内时,轻量工具调用仍秒级返回(#458)。"""
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())

        entered = threading.Event()
        release = threading.Event()
        original = reporting.aggregate_run_report

        def _slow_aggregate(
            row: Any,
            artifacts: Any,
            *,
            view: str = "summary",
            decision_id: Any = None,
            artifact_summary: Any = None,
        ) -> Any:
            if view == "detail":
                entered.set()
                assert release.wait(timeout=10)
            return original(
                row,
                artifacts,
                view=view,
                decision_id=decision_id,
                artifact_summary=artifact_summary,
            )

        monkeypatch.setattr(reporting, "aggregate_run_report", _slow_aggregate)
        monkeypatch.setattr(
            ResearchStrategySpecRepository,
            "get_version",
            lambda self, sid, v: _async_return(_spec_row()),
        )
        monkeypatch.setattr(strategy_tools, "_version_to_dict", _passthrough_row)

        heavy = asyncio.create_task(
            rp_tools.report_run(app, _RUN_ID, view="detail")
        )
        try:
            # 确定性等待:heavy 已进入线程内的聚合段(不靠 sleep 时长猜)。
            assert await asyncio.to_thread(entered.wait, 10.0)
            light = await asyncio.wait_for(
                strategy_tools.strategy_version_get(
                    app, strategy_id="mf_test", version=1
                ),
                timeout=2.0,
            )
            assert light.status == "ok", light.error
            assert light.data["version"] == 1
            # heavy 仍停在线程内 —— 证明它没有阻塞事件循环。
            assert not heavy.done()
        finally:
            release.set()
        heavy_env = await asyncio.wait_for(heavy, timeout=10.0)
        assert heavy_env.status == "ok", heavy_env.error


# ---------------------------------------------------------------------------
# report_export 护栏与下钻
# ---------------------------------------------------------------------------


class TestReportExportGuard:
    async def test_export_run_over_limit_raises(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        artifacts = [
            _artifact(
                artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
                sequence=1,
                stage="features",
                decision_id="2026-01-05",
                payload={"blob": "y" * 64},
            ),
        ]
        _patch_run_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_export(app, "run", _RUN_ID, "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert list(tmp_path.iterdir()) == []  # 不写半成品文件

    async def test_export_run_decision_id_filters_and_bypasses_guard(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        _patch_run_repos(monkeypatch, _run_row(), _drilldown_artifacts())
        env = await rp_tools.report_export(
            app, "run", _RUN_ID, "csv", decision_id="2026-01-05"
        )
        assert env.status == "ok", env.error
        text = Path(env.data["path"]).read_text(encoding="utf-8-sig")  # noqa: ASYNC240
        assert "# artifacts" in text
        assert "2026-01-05" in text
        assert "2026-02-02" not in text  # 只导出目标决策

    async def test_export_backtest_decision_id_rejected(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(
            app, "backtest", "1", "csv", decision_id="2026-01-05"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_export_backtest_zero_change(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """decision_id 不传时 backtest 导出行为零变化。"""
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        app = _make_app()
        _patch_backtest_repos(monkeypatch, _backtest_row())
        env = await rp_tools.report_export(app, "backtest", "1", "csv")
        assert env.status == "ok", env.error
        text = Path(env.data["path"]).read_text(encoding="utf-8-sig")  # noqa: ASYNC240
        assert "# equity_curve" in text
        assert "sharpe_ratio,1.5" in text


# ---------------------------------------------------------------------------
# reporting 纯函数回归(summary 逐字段锁定)
# ---------------------------------------------------------------------------


class TestReportingRegression:
    def test_aggregate_summary_field_by_field(self) -> None:
        """summary 输出与改动前逐字段相等(计数口径不变)。"""
        row = _run_row(
            result={
                "strategy_return": 0.42,
                "equity_curve": [{"date": "2026-01-05"}],
            }
        )
        artifacts = [
            _artifact(
                artifact_id="a-universe",
                sequence=1,
                stage="universe",
                decision_id="D1",
                payload={
                    "candidates": [
                        {"symbol": "A", "included": True},
                        {"symbol": "B", "included": False, "reasons": ["below_price"]},
                        {
                            "symbol": "C",
                            "included": False,
                            "reasons": ["below_price", "illiquid"],
                        },
                    ]
                },
            ),
            _artifact(
                artifact_id="a-fills",
                sequence=2,
                stage="fills",
                decision_id="D1",
                payload={"fills": [{"symbol": "A"}, {"symbol": "B"}]},
            ),
            _artifact(
                artifact_id="a-fills2",
                sequence=3,
                stage="fills",
                decision_id="D2",
                payload={"fills": [{"symbol": "C"}]},
            ),
            _artifact(
                artifact_id="a-other",
                sequence=4,
                stage="features",
                decision_id="D2",
                payload={"big": "z" * 500},
            ),
        ]
        report = reporting.aggregate_run_report(row, artifacts, view="summary")
        assert report == {
            "run_id": _RUN_ID,
            "status": "completed",
            "strategy_id": "strat-demo-001",
            "strategy_kind": "ma_cross",
            "created_at": "2026-01-15T08:30:00+00:00",
            "started_at": "2026-01-15T08:31:00+00:00",
            "completed_at": "2026-01-15T08:35:00+00:00",
            "error_code": None,
            "error_summary": None,
            "metrics": {"strategy_return": 0.42, "equity_point_count": 1},
            "artifact_count": 4,
            "view": "summary",
            "universe": {
                "total": 3,
                "included": 1,
                "excluded_by_reason": {"below_price": 2, "illiquid": 1},
            },
            "fills": {"total": 3, "by_decision": {"D1": 2, "D2": 1}},
        }

    def test_detail_without_decision_id_no_new_keys(self) -> None:
        """未下钻的 detail 输出键集合与旧版本一致(REST 导出零漂移)。"""
        report = reporting.aggregate_run_report(
            _run_row(),
            [_artifact(artifact_id="a", sequence=1, stage="report")],
            view="detail",
        )
        assert set(report) == {
            "run_id",
            "status",
            "strategy_id",
            "strategy_kind",
            "created_at",
            "started_at",
            "completed_at",
            "error_code",
            "error_summary",
            "metrics",
            "artifact_count",
            "view",
            "artifacts",
        }

    def test_summary_with_decision_id_value_error(self) -> None:
        with pytest.raises(ValueError, match="decision_id"):
            reporting.aggregate_run_report(
                _run_row(), [], view="summary", decision_id="D1"
            )

    def test_estimate_run_detail_bytes(self) -> None:
        artifacts = [
            _artifact(
                artifact_id="a1",
                sequence=1,
                stage="features",
                payload={"x": "y" * 10},
            ),
            # payload 为 null 的行不参与估计(estimate 跳过 falsy payload)。
            ResearchRunArtifactModel(
                run_id=_RUN_ID,
                artifact_id="a2",
                decision_id=None,
                sequence=2,
                stage="report",
                trace_id="RRT-0002",
                parent_trace_ids=[],
                payload=None,
                checksum="c2",
                created_at=datetime(2026, 1, 15, 8, 35, tzinfo=UTC),
            ),
        ]
        assert reporting.estimate_run_detail_bytes(artifacts) == len(
            str({"x": "y" * 10})
        )


# ---------------------------------------------------------------------------
# finboard_factor_series_get detail 护栏(#458 同款)
# ---------------------------------------------------------------------------


def _series_record() -> FactorSeriesRecord:
    return FactorSeriesRecord.build(
        code_artifact="mom20",
        code_commit=_COMMIT,
        kind="factor",
        release_id="DR-bars-1",
        dataset_release_ids=("DR-fin-1",),
        params={},
        window_start=date(2022, 1, 1),
        window_end=date(2023, 6, 30),
        dates=[date(2024, 1, 31)],
        values={"2024-01-31": {"600000.SH": 1.0}},
    )


def _fake_series_get(record: Any):
    async def _get(self: Any, series_id: str) -> Any:
        return record

    return _get


class TestFactorSeriesGetGuard:
    async def test_series_detail_over_limit_raises(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(fs_tools, "SERIES_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        record = _series_record()
        with patch.object(FactorSeriesRepository, "get", _fake_series_get(record)):
            env = await fs_tools.series_get(app, record.series_id, view="detail")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert "view=summary" in env.error.message

    async def test_series_summary_unaffected_by_guard(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(fs_tools, "SERIES_DETAIL_MAX_ESTIMATED_BYTES", 8)
        app = _make_app()
        record = _series_record()
        with patch.object(FactorSeriesRepository, "get", _fake_series_get(record)):
            env = await fs_tools.series_get(app, record.series_id)
        assert env.status == "ok", env.error
        assert env.data["date_count"] == 1
        assert "values" not in env.data

    async def test_series_detail_under_limit_ok(self) -> None:
        app = _make_app()
        record = _series_record()
        with patch.object(FactorSeriesRepository, "get", _fake_series_get(record)):
            env = await fs_tools.series_get(app, record.series_id, view="detail")
        assert env.status == "ok", env.error
        assert env.data["values"] == {"2024-01-31": {"600000.SH": 1.0}}
        assert env.data["dates"] == ["2024-01-31"]
