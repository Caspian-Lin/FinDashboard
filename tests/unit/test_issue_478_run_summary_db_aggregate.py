"""``finboard_run_get`` / ``finboard_report_run`` summary 数据库侧有界聚合(issue #478,mock repo)。

背景:RR-169678a2196b8842ac8f28fb(7203 artifacts / ≈5.9GB JSON)的
``view=summary`` 曾先全量物化 artifacts 再 Python 计数,把 FinDashboard
API/MCP 进程从 ~170MB 顶到 13.27GB,OpenCode 侧报 MCP error -32001。

修复:summary 聚合下沉 PostgreSQL(``ResearchRunRepository.summarize_artifacts``),
只返回有界计数字段。本文件验证(mock 仓储层,无需 DB):

* 接线:summary 只调 ``summarize_artifacts``、绝不调 ``list_artifacts``;
  detail 两者都不调;
* 等值:``_run_view`` / ``aggregate_run_report`` 的 ``artifact_summary``
  路径输出与传全量 artifacts 的 Python 聚合逐字段一致;
* 契约:不存在 run(not_found)/ 非法 view(invalid_argument)语义不变;
  空 run(0 artifacts)聚合为零值不崩;``summary_dict`` 返回副本。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp import reporting
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import reports as rp_tools
from finboard_mcp.tools import runs
from finboard_persistence import (
    ResearchRunArtifactModel,
    ResearchRunArtifactSummary,
    ResearchRunModel,
    ResearchRunRepository,
)

_RUN_ID = "RR-478aabbccddeeff00112233"


async def _async_return(value: object) -> object:
    return value


def _run_row() -> ResearchRunModel:
    return ResearchRunModel(
        run_id=_RUN_ID,
        idempotency_key=f"ik-{_RUN_ID}",
        strategy_id="strat-demo-001",
        strategy_kind="multi_factor",
        status="completed",
        schema_version="1.0",
        manifest_checksum="abc123",
        manifest={"ok": True},
        result={"strategy_return": 0.5},
        requested_by="unit-test",
        created_at=datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
        started_at=datetime(2026, 9, 15, 8, 31, tzinfo=UTC),
        completed_at=datetime(2026, 9, 15, 8, 35, tzinfo=UTC),
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
        created_at=datetime(2026, 9, 15, 8, 35, tzinfo=UTC),
    )


def _sample_artifacts() -> list[ResearchRunArtifactModel]:
    """覆盖:多决策、空 / 多 / 缺失 / None reasons、decision_id=None、
    非 universe/fills stage(features/report 只进 artifact_count)。"""
    return [
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:universe",
            sequence=1,
            stage="universe",
            decision_id="D1",
            payload={
                "candidates": [
                    {"symbol": "A", "included": True, "reasons": ["ignored"]},
                    {"symbol": "B", "included": False, "reasons": ["st"]},
                    {"symbol": "C", "included": False, "reasons": ["st", "thin"]},
                    {"symbol": "D", "included": False, "reasons": []},
                    {"symbol": "E", "included": False},
                ]
            },
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D2:universe",
            sequence=2,
            stage="universe",
            decision_id="D2",
            payload={
                "candidates": [
                    {"symbol": "F", "included": False, "reasons": None},
                    {"symbol": "G", "included": False, "reasons": ["late"]},
                ]
            },
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:fills",
            sequence=3,
            stage="fills",
            decision_id="D1",
            payload={"fills": [{"symbol": "A"}, {"symbol": "B"}]},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D2:fills",
            sequence=4,
            stage="fills",
            decision_id="D2",
            payload={"fills": []},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D3:fills",
            sequence=5,
            stage="fills",
            decision_id=None,
            payload={"fills": [{"symbol": "C"}]},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:D1:features",
            sequence=6,
            stage="features",
            decision_id="D1",
            payload={"features": {"A": {"momentum": 0.1}}},
        ),
        _artifact(
            artifact_id=f"{_RUN_ID}:A:report",
            sequence=7,
            stage="report",
            decision_id=None,
            payload={"report": {"strategy_return": 0.5}},
        ),
    ]


def _db_summary_of(
    artifacts: list[ResearchRunArtifactModel],
) -> ResearchRunArtifactSummary:
    """按 Python 参考聚合构造 ``summarize_artifacts`` 的等价返回(#478 契约)。"""
    agg = reporting.summarize_run_artifacts(artifacts)
    return ResearchRunArtifactSummary(
        artifact_count=len(artifacts),
        universe_total=agg["universe"]["total"],
        universe_included=agg["universe"]["included"],
        universe_excluded_by_reason=dict(agg["universe"]["excluded_by_reason"]),
        fills_total=agg["fills"]["total"],
        fills_by_decision=dict(agg["fills"]["by_decision"]),
    )


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


def _patch_repos(
    monkeypatch: pytest.MonkeyPatch,
    row: ResearchRunModel | None,
    artifacts: list[ResearchRunArtifactModel],
) -> dict[str, int]:
    """类级桩 get / summarize_artifacts / list_artifacts,并记录调用次数。"""
    calls = {"get": 0, "summarize": 0, "list": 0}

    async def _get(self: ResearchRunRepository, rid: str) -> ResearchRunModel | None:
        calls["get"] += 1
        return row

    async def _summarize(
        self: ResearchRunRepository, rid: str
    ) -> ResearchRunArtifactSummary:
        calls["summarize"] += 1
        return _db_summary_of(artifacts)

    async def _list(
        self: ResearchRunRepository, rid: str
    ) -> list[ResearchRunArtifactModel]:
        calls["list"] += 1
        return artifacts

    monkeypatch.setattr(ResearchRunRepository, "get", _get)
    monkeypatch.setattr(ResearchRunRepository, "summarize_artifacts", _summarize)
    monkeypatch.setattr(ResearchRunRepository, "list_artifacts", _list)
    return calls


class TestGetRunWiring:
    async def test_summary_uses_db_aggregate_never_full_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """summary 只走 summarize_artifacts,绝不触发全量 list_artifacts。"""
        artifacts = _sample_artifacts()
        calls = _patch_repos(monkeypatch, _run_row(), artifacts)
        env = await runs.get_run(_make_app(), _RUN_ID)
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 1, "list": 0}
        data = env.data
        assert data["view"] == "summary"
        assert data["artifact_count"] == len(artifacts)
        assert data["universe"] == {
            "total": 7,
            "included": 1,
            "excluded_by_reason": {"st": 2, "thin": 1, "unknown": 3, "late": 1},
        }
        assert data["fills"] == {
            "total": 3,
            "by_decision": {"D1": 2, "D2": 0, "": 1},
        }

    async def test_summary_output_equals_python_aggregate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifacts = _sample_artifacts()
        _patch_repos(monkeypatch, _run_row(), artifacts)
        env = await runs.get_run(_make_app(), _RUN_ID)
        reference = _run_row()
        payload = dict(env.data)
        python_view = runs._run_view(reference, artifacts, view="summary")
        assert payload == python_view

    async def test_detail_touches_neither_aggregate_nor_full_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, _run_row(), _sample_artifacts())
        env = await runs.get_run(_make_app(), _RUN_ID, view="detail")
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 0, "list": 0}
        assert env.data["manifest"] == {"ok": True}
        assert "universe" not in env.data

    async def test_not_found_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, None, [])
        env = await runs.get_run(_make_app(), "RR-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
        assert calls == {"get": 1, "summarize": 0, "list": 0}

    async def test_invalid_view_named_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, _run_row(), [])
        env = await runs.get_run(_make_app(), _RUN_ID, view="huge")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        # get 先行(与旧路径一致),聚合类调用零触发
        assert calls == {"get": 1, "summarize": 0, "list": 0}

    async def test_zero_artifact_run_zero_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_repos(monkeypatch, _run_row(), [])
        env = await runs.get_run(_make_app(), _RUN_ID)
        assert env.status == "ok", env.error
        assert env.data["artifact_count"] == 0
        assert env.data["universe"] == {
            "total": 0,
            "included": 0,
            "excluded_by_reason": {},
        }
        assert env.data["fills"] == {"total": 0, "by_decision": {}}


class TestReportRunWiring:
    async def test_report_summary_uses_db_aggregate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifacts = _sample_artifacts()
        calls = _patch_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_run(_make_app(), _RUN_ID, view="summary")
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 1, "list": 0}
        assert env.data["artifact_count"] == len(artifacts)
        assert env.data["universe"]["total"] == 7
        assert "artifacts" not in env.data

    async def test_report_detail_still_full_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifacts = _sample_artifacts()
        calls = _patch_repos(monkeypatch, _run_row(), artifacts)
        env = await rp_tools.report_run(_make_app(), _RUN_ID, view="detail")
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 0, "list": 1}
        assert len(env.data["artifacts"]) == len(artifacts)


class TestAggregateParity:
    def test_run_view_artifact_summary_matches_python(self) -> None:
        row = _run_row()
        artifacts = _sample_artifacts()
        summary = _db_summary_of(artifacts)
        via_artifacts = runs._run_view(row, artifacts, view="summary")
        via_summary = runs._run_view(row, view="summary", artifact_summary=summary)
        assert via_summary == via_artifacts

    def test_aggregate_run_report_summary_matches_python(self) -> None:
        row = _run_row()
        artifacts = _sample_artifacts()
        summary = _db_summary_of(artifacts)
        via_artifacts = reporting.aggregate_run_report(row, artifacts, view="summary")
        via_summary = reporting.aggregate_run_report(
            row, None, view="summary", artifact_summary=summary
        )
        assert via_summary == via_artifacts

    def test_aggregate_requires_a_source(self) -> None:
        with pytest.raises(ValueError, match="至少提供一个"):
            reporting.aggregate_run_report(_run_row(), None, view="summary")

    def test_aggregate_detail_rejects_summary_only_path(self) -> None:
        summary = _db_summary_of(_sample_artifacts())
        with pytest.raises(ValueError, match="仅 view=summary"):
            reporting.aggregate_run_report(
                _run_row(), None, view="detail", artifact_summary=summary
            )

    def test_summary_dict_returns_copies(self) -> None:
        summary = _db_summary_of(_sample_artifacts())
        dumped = summary.summary_dict()
        dumped["universe"]["excluded_by_reason"]["st"] = 999
        dumped["fills"]["by_decision"]["D1"] = 999
        assert summary.universe_excluded_by_reason["st"] == 2
        assert summary.fills_by_decision["D1"] == 2
