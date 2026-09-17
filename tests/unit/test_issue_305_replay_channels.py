"""issue #305:run_replay 放开 interrupted —— MCP / REST 层守卫与继承一致性。

Coordinator 是权威判定(``REPLAYABLE_SOURCE_STATUSES``),MCP 与 REST 守卫
复用同一判定与同一错误文案(``replay_guard_error``)。本文件用 mock store
锁定三处一致:

* interrupted 源:守卫放行,新 run 自动继承冻结输入(input_checksum 相等、
  factor_snapshots 全集零手工),血缘标注 replay_of_run_id +
  replay_source_status;
* cancelled 源:仍拒绝,错误区分状态不允许并给出 finboard_run_replay 恢复说明;
* completed 源:血缘标注 replay_source_status="completed"。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_api.deps import get_db_session
from finboard_api.routes import research_runs_router
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_run import (
    FrozenArtifactRef,
    ResearchActorType,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunStatus,
    stable_checksum,
    to_json_value,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import runs
from finboard_persistence.models import ResearchRunModel
from finboard_persistence.research_run_repo import ResearchRunRepository

_SOURCE_RUN_ID = "RR-305-source-000001"


# ---------------------------------------------------------------------------
# 固定样本构建
# ---------------------------------------------------------------------------


def _manifest(run_id: str = _SOURCE_RUN_ID) -> ResearchRunManifest:
    from finboard_backtest.strategy_spec import build_strategy_template

    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_research",
        dataset_release_ids=("release-v1",),
    )
    return ResearchRunManifest(
        run_id=run_id,
        idempotency_key="idem-305-source-001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-v1",
                version="2026-01-01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(
            FrozenArtifactRef(
                artifact_id="factor-v1",
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:close",),
            ),
            FrozenArtifactRef(
                artifact_id="factor-v2",
                version="v1",
                checksum="c" * 64,
                capabilities=("factor:momentum",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
        actor_type=ResearchActorType.HUMAN,
    )


def _record(
    manifest: ResearchRunManifest, status: ResearchRunStatus
) -> ResearchRunRecord:
    return ResearchRunRecord(manifest=manifest, status=status)


def _manifest_payload(manifest: ResearchRunManifest) -> dict[str, object]:
    payload = to_json_value(manifest)
    assert isinstance(payload, dict)
    return cast("dict[str, object]", payload)


def _row_model(manifest: ResearchRunManifest, job_id: str = "BJ-305-1") -> ResearchRunModel:
    return ResearchRunModel(
        run_id=manifest.run_id,
        idempotency_key=manifest.idempotency_key,
        replay_of_run_id=manifest.replay_of_run_id,
        strategy_id=manifest.strategy_spec.strategy_id,
        strategy_kind=manifest.strategy_kind,
        status=ResearchRunStatus.QUEUED.value,
        schema_version=manifest.schema_version,
        manifest_checksum=manifest.checksum,
        manifest=_manifest_payload(manifest),
        result=None,
        result_checksum=None,
        error_code=None,
        error_summary=None,
        requested_by=manifest.requested_by,
        job_id=job_id,
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        updated_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def _new_run_id(idempotency_key: str) -> str:
    return "RR-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# MCP:finboard_run_replay
# ---------------------------------------------------------------------------


def _session_maker() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app() -> McpAppContext:
    from finboard_app.config import Settings
    from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager

    return McpAppContext(
        settings=Settings(),
        session_maker=_session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


class TestMcpReplayInterrupted:
    async def test_not_found_distinct_from_status_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        async def _fake_get(self: object, run_id: str) -> None:
            return None

        monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
        env = await runs.replay_run(
            app, "RR-missing", idempotency_key="idem-305-mcp-01", requested_by="u"
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_cancelled_rejected_with_shared_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        cancelled = _record(_manifest(), ResearchRunStatus.CANCELLED)

        async def _fake_get(self: object, run_id: str) -> ResearchRunRecord:
            return cancelled

        monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
        env = await runs.replay_run(
            app,
            cancelled.manifest.run_id,
            idempotency_key="idem-305-mcp-02",
            requested_by="u",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"
        message = env.error.message or ""
        assert "interrupted" in message
        assert "finboard_run_replay" in message

    async def test_interrupted_accepted_and_inherits_frozen_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        source_manifest = _manifest()
        source = _record(source_manifest, ResearchRunStatus.INTERRUPTED)
        captured: dict[str, ResearchRunManifest] = {}

        async def _fake_get(self: object, run_id: str) -> ResearchRunRecord:
            assert run_id == source_manifest.run_id
            return source

        async def _fake_create_or_get(
            self: object, manifest: ResearchRunManifest
        ) -> tuple[ResearchRunRecord, bool]:
            captured["manifest"] = manifest
            return _record(manifest, ResearchRunStatus.QUEUED), True

        row = _row_model(
            replace(
                source_manifest,
                run_id=_new_run_id("idem-305-mcp-03"),
                idempotency_key="idem-305-mcp-03",
            )
        )

        async def _fake_repo_get(self: object, run_id: str) -> ResearchRunModel:
            return row

        monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
        monkeypatch.setattr(
            SqlAlchemyResearchRunStore, "create_or_get", _fake_create_or_get
        )
        monkeypatch.setattr(ResearchRunRepository, "get", _fake_repo_get)

        env = await runs.replay_run(
            app,
            source_manifest.run_id,
            idempotency_key="idem-305-mcp-03",
            requested_by="user:305",
        )

        assert env.status == "ok"
        new_manifest = captured["manifest"]
        # 新 run 身份与血缘标注(replay-of-interrupted)。
        assert new_manifest.run_id == _new_run_id("idem-305-mcp-03")
        assert new_manifest.replay_of_run_id == source_manifest.run_id
        assert new_manifest.replay_source_status == "interrupted"
        assert new_manifest.requested_by == "user:305"
        # issue #312:MCP 通道重放归属 agent(与 requested_by 语义对齐)。
        assert new_manifest.actor_type is ResearchActorType.AGENT
        # 冻结输入零手工继承:input_checksum 相等 + 快照全集一致。
        assert new_manifest.input_checksum == source_manifest.input_checksum
        assert new_manifest.factor_snapshots == source_manifest.factor_snapshots
        assert new_manifest.dataset_releases == source_manifest.dataset_releases

    async def test_completed_annotates_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        source_manifest = _manifest()
        source = _record(source_manifest, ResearchRunStatus.COMPLETED)
        captured: dict[str, ResearchRunManifest] = {}

        async def _fake_get(self: object, run_id: str) -> ResearchRunRecord:
            return source

        async def _fake_create_or_get(
            self: object, manifest: ResearchRunManifest
        ) -> tuple[ResearchRunRecord, bool]:
            captured["manifest"] = manifest
            return _record(manifest, ResearchRunStatus.QUEUED), True

        row = _row_model(
            replace(
                source_manifest,
                run_id=_new_run_id("idem-305-mcp-04"),
                idempotency_key="idem-305-mcp-04",
            )
        )

        async def _fake_repo_get(self: object, run_id: str) -> ResearchRunModel:
            return row

        monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
        monkeypatch.setattr(
            SqlAlchemyResearchRunStore, "create_or_get", _fake_create_or_get
        )
        monkeypatch.setattr(ResearchRunRepository, "get", _fake_repo_get)

        env = await runs.replay_run(
            app,
            source_manifest.run_id,
            idempotency_key="idem-305-mcp-04",
            requested_by="user:305",
        )

        assert env.status == "ok"
        assert captured["manifest"].replay_source_status == "completed"
        assert captured["manifest"].replay_of_run_id == source_manifest.run_id


# ---------------------------------------------------------------------------
# REST:POST /api/research/runs/{run_id}/replay
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_client() -> TestClient:
    app = FastAPI()
    app.include_router(research_runs_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def _patch_rest_store(
    monkeypatch: pytest.MonkeyPatch,
    source: ResearchRunRecord | None,
    *,
    captured: list[ResearchRunManifest] | None = None,
    row: ResearchRunModel | None = None,
) -> None:
    async def _fake_get(self: object, run_id: str) -> ResearchRunRecord | None:
        return source

    async def _fake_create_or_get(
        self: object, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]:
        if captured is not None:
            captured.append(manifest)
        return _record(manifest, ResearchRunStatus.QUEUED), True

    async def _fake_repo_get(self: object, run_id: str) -> ResearchRunModel | None:
        # 回执行以最近一次捕获的重放 manifest 构建(血缘标注可见)。
        if captured:
            return _row_model(captured[-1])
        return row

    monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
    monkeypatch.setattr(SqlAlchemyResearchRunStore, "create_or_get", _fake_create_or_get)
    monkeypatch.setattr(ResearchRunRepository, "get", _fake_repo_get)


class TestRestReplayInterrupted:
    def test_cancelled_rejected_with_shared_message(
        self, rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cancelled = _record(_manifest(), ResearchRunStatus.CANCELLED)
        _patch_rest_store(monkeypatch, cancelled)
        response = rest_client.post(
            f"/api/research/runs/{cancelled.manifest.run_id}/replay",
            json={"idempotency_key": "idem-305-rest-01", "requested_by": "user:305"},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "interrupted" in detail
        assert "finboard_run_replay" in detail

    def test_interrupted_accepted_and_inherits_frozen_inputs(
        self, rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_manifest = _manifest()
        source = _record(source_manifest, ResearchRunStatus.INTERRUPTED)
        captured: list[ResearchRunManifest] = []
        _patch_rest_store(monkeypatch, source, captured=captured)
        response = rest_client.post(
            f"/api/research/runs/{source_manifest.run_id}/replay",
            json={"idempotency_key": "idem-305-rest-02", "requested_by": "user:305"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "queued"
        new_manifest = captured[0]
        assert new_manifest.run_id == _new_run_id("idem-305-rest-02")
        assert new_manifest.replay_of_run_id == source_manifest.run_id
        assert new_manifest.replay_source_status == "interrupted"
        assert new_manifest.input_checksum == source_manifest.input_checksum
        assert new_manifest.factor_snapshots == source_manifest.factor_snapshots
        assert new_manifest.dataset_releases == source_manifest.dataset_releases
        # 响应携带血缘标注(manifest JSON 原样出参)。
        assert body["manifest"]["replay_source_status"] == "interrupted"
        assert body["replay_of_run_id"] == source_manifest.run_id

    def test_not_found_is_404(
        self, rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_rest_store(monkeypatch, None)
        response = rest_client.post(
            "/api/research/runs/RR-missing/replay",
            json={"idempotency_key": "idem-305-rest-03", "requested_by": "user:305"},
        )
        assert response.status_code == 404
