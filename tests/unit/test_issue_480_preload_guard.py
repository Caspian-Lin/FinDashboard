"""report detail / export 载荷护栏前移:数据库侧估计、加载前拒绝(issue #480,mock repo)。

背景:#458 的护栏在 ``list_artifacts`` 全量物化之后才估计,只能拒绝序列化、
挡不住加载本身 —— 真实 run(7203 artifacts / ≈5.9GB JSON)实测加载阶段
113s 就把进程顶到 10GB+。修复:先 ``estimate_artifact_payload_bytes``
(SQL 侧 SUM(octet_length),不取回 payload),超限具名 ``payload_too_large``
且绝不触发全量加载;下钻单决策有界,维持豁免。

本文件(mock 仓储层)锁定接线:

* detail / export 超限 → ``estimate`` 恰一次、``list_artifacts`` 零调用;
* 未超限 → 估计后照旧加载聚合;
* ``decision_id`` 下钻 → 豁免估计(有界),直接加载;
* run 不存在 → not_found 短路,不触发估计。
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

_RUN_ID = "RR-480aabbccddeeff00112233"


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
        created_at=datetime(2026, 9, 16, 8, 30, tzinfo=UTC),
        started_at=datetime(2026, 9, 16, 8, 31, tzinfo=UTC),
        completed_at=datetime(2026, 9, 16, 8, 35, tzinfo=UTC),
    )


def _artifact(
    *,
    artifact_id: str,
    sequence: int,
    stage: str = "features",
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
        created_at=datetime(2026, 9, 16, 8, 35, tzinfo=UTC),
    )


def _big_artifacts() -> list[ResearchRunArtifactModel]:
    return [
        _artifact(
            artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
            sequence=1,
            decision_id="2026-01-05",
            payload={"blob": "x" * 4096},
        ),
    ]


def _small_artifacts() -> list[ResearchRunArtifactModel]:
    return [
        _artifact(
            artifact_id=f"{_RUN_ID}:A:2026-01-05:features",
            sequence=1,
            decision_id="2026-01-05",
            payload={"report": {"strategy_return": 0.5}},
        ),
    ]


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
    calls = {"get": 0, "summarize": 0, "estimate": 0, "list": 0}

    async def _get(self: Any, rid: Any) -> ResearchRunModel | None:
        calls["get"] += 1
        return row

    async def _summarize(self: Any, rid: Any) -> ResearchRunArtifactSummary:
        calls["summarize"] += 1
        aggregate = reporting.summarize_run_artifacts(artifacts)
        return ResearchRunArtifactSummary(
            artifact_count=len(artifacts),
            universe_total=aggregate["universe"]["total"],
            universe_included=aggregate["universe"]["included"],
            universe_excluded_by_reason=dict(
                aggregate["universe"]["excluded_by_reason"]
            ),
            fills_total=aggregate["fills"]["total"],
            fills_by_decision=dict(aggregate["fills"]["by_decision"]),
        )

    async def _estimate(self: Any, rid: Any) -> int:
        calls["estimate"] += 1
        return sum(len(str(a.payload)) for a in artifacts if a.payload)

    async def _list(self: Any, rid: Any) -> list[ResearchRunArtifactModel]:
        calls["list"] += 1
        return artifacts

    monkeypatch.setattr(ResearchRunRepository, "get", _get)
    monkeypatch.setattr(ResearchRunRepository, "summarize_artifacts", _summarize)
    monkeypatch.setattr(
        ResearchRunRepository, "estimate_artifact_payload_bytes", _estimate
    )
    monkeypatch.setattr(ResearchRunRepository, "list_artifacts", _list)
    return calls


class TestReportRunPreloadGuard:
    async def test_detail_over_limit_refuses_before_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 128)
        calls = _patch_repos(monkeypatch, _run_row(), _big_artifacts())
        env = await rp_tools.report_run(_make_app(), _RUN_ID, view="detail")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert calls == {"get": 1, "summarize": 0, "estimate": 1, "list": 0}

    async def test_detail_under_limit_loads_after_estimate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, _run_row(), _small_artifacts())
        env = await rp_tools.report_run(_make_app(), _RUN_ID, view="detail")
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 0, "estimate": 1, "list": 1}
        assert "artifacts" in env.data

    async def test_detail_drilldown_bypasses_estimate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        calls = _patch_repos(monkeypatch, _run_row(), _small_artifacts())
        env = await rp_tools.report_run(
            _make_app(), _RUN_ID, view="detail", decision_id="2026-01-05"
        )
        assert env.status == "ok", env.error
        # 下钻单决策有界,豁免估计(#458 契约保持)。
        assert calls["estimate"] == 0
        assert calls["list"] == 1

    async def test_not_found_short_circuits_before_estimate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, None, [])
        env = await rp_tools.report_run(_make_app(), "RR-missing", view="detail")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
        assert calls == {"get": 1, "summarize": 0, "estimate": 0, "list": 0}


class TestReportExportPreloadGuard:
    async def test_export_over_limit_refuses_before_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 128)
        calls = _patch_repos(monkeypatch, _run_row(), _big_artifacts())
        env = await rp_tools.report_export(_make_app(), "run", _RUN_ID, "csv")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert list(tmp_path.iterdir()) == []  # 不写半成品文件
        assert calls == {"get": 1, "summarize": 0, "estimate": 1, "list": 0}

    async def test_export_drilldown_bypasses_estimate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("FINBOARD_EXPORT_DIR", str(tmp_path))
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 8)
        calls = _patch_repos(monkeypatch, _run_row(), _small_artifacts())
        env = await rp_tools.report_export(
            _make_app(), "run", _RUN_ID, "csv", decision_id="2026-01-05"
        )
        assert env.status == "ok", env.error
        # 豁免估计,但仍需加载做下钻过滤。
        assert calls["estimate"] == 0
        assert calls["list"] == 1


class TestRunArtifactsPreloadGuard:
    """``finboard_run_artifacts`` 全量 payload 列表的加载前护栏(#480)。"""

    async def test_over_limit_refuses_before_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 128)
        calls = _patch_repos(monkeypatch, _run_row(), _big_artifacts())
        env = await runs.list_artifacts(_make_app(), _RUN_ID)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert calls == {"get": 1, "summarize": 0, "estimate": 1, "list": 0}

    async def test_under_limit_lists_with_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, _run_row(), _small_artifacts())
        env = await runs.list_artifacts(_make_app(), _RUN_ID)
        assert env.status == "ok", env.error
        assert calls == {"get": 1, "summarize": 0, "estimate": 1, "list": 1}
        assert isinstance(env.data, list)
        assert env.data[0]["payload"] == {"report": {"strategy_return": 0.5}}

    async def test_not_found_short_circuits_before_estimate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_repos(monkeypatch, None, [])
        env = await runs.list_artifacts(_make_app(), "RR-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
        assert calls == {"get": 1, "summarize": 0, "estimate": 0, "list": 0}


class TestLineagePreloadGuard:
    """``finboard_run_lineage`` 的加载前护栏(#480;coordinator 内部全量物化)。"""

    async def test_over_limit_refuses_before_lineage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reporting, "RUN_DETAIL_MAX_ESTIMATED_BYTES", 128)
        calls = _patch_repos(monkeypatch, _run_row(), _big_artifacts())

        async def _boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("超限时不应触达 coordinator.lineage")

        monkeypatch.setattr(
            "finboard_backtest.research_run.ResearchRunCoordinator.lineage", _boom
        )
        env = await runs.lineage_run(_make_app(), _RUN_ID, "RRT-0001")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "payload_too_large"
        assert calls == {"get": 0, "summarize": 0, "estimate": 1, "list": 0}
