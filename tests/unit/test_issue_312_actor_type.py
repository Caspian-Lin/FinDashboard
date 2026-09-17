"""issue #312:actor_type 与 requested_by 语义对齐 —— MCP 通道 actor_type=agent。

#122 已放开外置研究 agent 经 MCP 自主执行研究写操作,但 run 记录曾硬编码
``actor_type="human"``、与同条记录 ``requested_by=agent:mcp`` 自相矛盾。
本文件锁定对齐后的契约:

* 契约层:``ResearchActorType`` 扩 ``agent``;llm 仍被 fail-closed 拒绝
  (红线不变);manifest 序列化 / ``manifest_from_json`` 对新枚举值往返一致,
  历史 ``"human"`` 载荷不受影响;
* MCP 通道:queue(``_build_queued_manifest``)与 replay 的 run 归属
  ``agent``;
* REST 通道:schema 默认 human 不变、放开 human|agent 二选一,llm 在
  schema 层即 422;replay 缺省(不带 actor_type)保持 human。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finboard_backtest.research_code as _research_code_mod
import finboard_persistence
from finboard_api.deps import get_db_session
from finboard_api.research_run_schemas import (
    ResearchRunQueueIn,
    ResearchRunReplayIn,
)
from finboard_api.routes import research_runs_router
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_run import (
    FrozenArtifactRef,
    ResearchActorType,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunStatus,
    manifest_from_json,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleasedInstrument,
    default_execution_metadata,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import runs
from finboard_persistence import ResearchRunRepository
from finboard_persistence.models import ResearchRunModel
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)

# ---------------------------------------------------------------------------
# 共用样本构建
# ---------------------------------------------------------------------------

_RUN_ID = "RR-312-source-000001"


def _manifest(run_id: str = _RUN_ID) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_research_312",
        dataset_release_ids=("release-312",),
    )
    return ResearchRunManifest(
        run_id=run_id,
        idempotency_key="idem-312-source-001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-312",
                version="2026-01-01",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test-312",
        actor_type=ResearchActorType.HUMAN,
    )


def _record(manifest: ResearchRunManifest, status: ResearchRunStatus) -> ResearchRunRecord:
    return ResearchRunRecord(manifest=manifest, status=status)


# ---------------------------------------------------------------------------
# 契约层:agent 放行 / llm 仍拒绝 / 序列化往返
# ---------------------------------------------------------------------------


class TestContractsAgentActor:
    def test_agent_actor_type_accepted(self) -> None:
        manifest = replace(_manifest(), actor_type=ResearchActorType.AGENT)
        assert manifest.actor_type is ResearchActorType.AGENT

    def test_llm_still_fail_closed(self) -> None:
        manifest = _manifest()
        with pytest.raises(ValueError, match="LLM"):
            replace(manifest, actor_type=ResearchActorType.LLM)

    def test_agent_roundtrip_via_payload(self) -> None:
        manifest = replace(_manifest(), actor_type=ResearchActorType.AGENT)
        payload = to_json_value(manifest)
        assert isinstance(payload, dict)
        assert payload["actor_type"] == "agent"
        restored = manifest_from_json(cast("Mapping[str, object]", payload))
        assert restored.actor_type is ResearchActorType.AGENT

    def test_historical_human_payload_still_parses(self) -> None:
        manifest = _manifest()
        payload = to_json_value(manifest)
        assert isinstance(payload, dict)
        assert payload["actor_type"] == "human"
        restored = manifest_from_json(cast("Mapping[str, object]", payload))
        assert restored.actor_type is ResearchActorType.HUMAN

    def test_actor_type_not_in_input_checksum(self) -> None:
        # actor_type 是归属标注而非冻结输入:切换 human/agent 不影响
        # 跨确定性重放的 input_checksum。
        human = _manifest()
        agent = replace(human, actor_type=ResearchActorType.AGENT)
        assert human.input_checksum == agent.input_checksum


# ---------------------------------------------------------------------------
# REST schema:默认 human 不变,放开 human|agent,llm 拒绝
# ---------------------------------------------------------------------------


def _queue_payload() -> dict[str, object]:
    return {
        "idempotency_key": "idem-312-queue-001",
        "strategy_id": "ma_cross_research_312",
        "strategy_version": 1,
        "dataset_release_ids": ["release-312"],
        "code_version": "abcdef0123456789",
        "initial_capital": "100000",
        "requested_by": "user:312",
    }


class TestRestSchemaActorType:
    def test_queue_defaults_to_human(self) -> None:
        body = ResearchRunQueueIn.model_validate(_queue_payload())
        assert body.actor_type == "human"

    def test_queue_accepts_agent(self) -> None:
        body = ResearchRunQueueIn.model_validate(
            {**_queue_payload(), "actor_type": "agent"}
        )
        assert body.actor_type == "agent"

    def test_queue_rejects_llm(self) -> None:
        with pytest.raises(ValidationError):
            ResearchRunQueueIn.model_validate(
                {**_queue_payload(), "actor_type": "llm"}
            )

    def test_replay_defaults_to_human(self) -> None:
        body = ResearchRunReplayIn.model_validate(
            {"idempotency_key": "idem-312-replay-01", "requested_by": "user:312"}
        )
        assert body.actor_type == "human"

    def test_replay_accepts_agent(self) -> None:
        body = ResearchRunReplayIn.model_validate(
            {
                "idempotency_key": "idem-312-replay-02",
                "requested_by": "agent:mcp",
                "actor_type": "agent",
            }
        )
        assert body.actor_type == "agent"

    def test_replay_rejects_llm(self) -> None:
        with pytest.raises(ValidationError):
            ResearchRunReplayIn.model_validate(
                {
                    "idempotency_key": "idem-312-replay-03",
                    "requested_by": "assistant",
                    "actor_type": "llm",
                }
            )


# ---------------------------------------------------------------------------
# MCP queue:_build_queued_manifest → actor_type=agent
# ---------------------------------------------------------------------------


def _release_312() -> ResearchDatasetRelease:
    instruments = tuple(
        ReleasedInstrument(
            code=code,
            name=code,
            name_history=(),
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            asset_class=AssetClass.EQUITY,
            available_at=datetime(2024, 1, 1, tzinfo=UTC),
            execution=default_execution_metadata(
                market=Market.A_SHARE,
                instrument_type=InstrumentType.STOCK,
            ),
            artifact_path=f"bars/{code}_D1_qfq.parquet",
            artifact_checksum="a" * 64,
            artifact_size=1024,
            row_count=240,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            expected_sessions=240,
            missing_sessions=0,
            suspended_sessions=0,
            anomaly_count=0,
            coverage_pct=Decimal("1.0"),
            category="stock",
            ready=True,
            list_date=date(2020, 1, 1),
            delist_date=None,
            industry=None,
        )
        for code in ("600001.SH", "600002.SH", "600003.SH")
    )
    return ResearchDatasetRelease(
        release_id="release-312",
        dataset_name="issue_312_bars",
        source="unit-test",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=instruments,
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(instruments),
                ready_count=len(instruments),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri="release-312",
        release_checksum="b" * 64,
    )


def _patch_queue_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``_build_queued_manifest`` 在调用期导入的持久化仓储替换为桩。

    函数体在调用时 ``from finboard_persistence import ...`` / ``from
    finboard_backtest.research_code import ...``,因此 monkeypatch 模块属性
    即可生效,无需真实 DB。
    """
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_research_312",
        dataset_release_ids=("release-312",),
    )
    # ma_cross 模板 universe 依赖 average_amount(min_average_amount /
    # required_data_fields / ranking);桩发布不携带成交额元数据,与
    # 集成测试用 multi_factor 模板规避同理,放宽后让候选池非空。
    spec = spec.model_copy(
        update={
            "universe": spec.universe.model_copy(
                update={
                    "min_average_amount": None,
                    "required_data_fields": ("price",),
                    "ranking": None,
                }
            )
        }
    )
    spec_checksum = stable_checksum(spec.canonical_payload())
    strategy_row = SimpleNamespace(
        status="published",
        payload=spec.canonical_payload(),
        checksum=spec_checksum,
    )
    release = _release_312()

    class _FakeSpecRepo:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_version(self, strategy_id: str, version: int) -> object:
            return strategy_row

    class _FakeReleaseRepo:
        def __init__(self, session: object) -> None:
            self.session = session

        async def require_usable(self, release_id: str) -> object:
            return release

    class _FakeFeatureRepo:
        def __init__(self, session: object) -> None:
            self.session = session

    async def _no_bindings(session: object, *, spec: object) -> tuple[None, None]:
        return None, None

    async def _no_user_factors(session: object) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(
        finboard_persistence, "ResearchStrategySpecRepository", _FakeSpecRepo
    )
    monkeypatch.setattr(
        finboard_persistence, "ResearchDatasetReleaseRepository", _FakeReleaseRepo
    )
    monkeypatch.setattr(
        finboard_persistence, "FeatureSnapshotRepository", _FakeFeatureRepo
    )
    monkeypatch.setattr(
        _research_code_mod, "resolve_screen_bindings", _no_bindings
    )
    monkeypatch.setattr(
        _research_code_mod, "active_user_factor_names", _no_user_factors
    )


@pytest.mark.asyncio
async def test_mcp_queue_manifest_actor_is_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_repos(monkeypatch)
    body = ResearchRunQueueIn.model_validate(_queue_payload())
    session = AsyncMock()

    manifest, _ = await runs._build_queued_manifest(body, session)

    # MCP 通道归属 agent(与 requested_by 同源 #122 语义对齐)。
    assert manifest.actor_type is ResearchActorType.AGENT
    assert manifest.requested_by == "user:312"


# ---------------------------------------------------------------------------
# MCP replay:actor_type=agent
# ---------------------------------------------------------------------------


def _session_maker() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _mcp_app() -> McpAppContext:
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


@pytest.mark.asyncio
async def test_mcp_replay_actor_is_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _mcp_app()
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

    row = ResearchRunModel(
        run_id="RR-312-replay-000001",
        idempotency_key="idem-312-replay-mcp",
        replay_of_run_id=source_manifest.run_id,
        strategy_id=source_manifest.strategy_spec.strategy_id,
        strategy_kind=source_manifest.strategy_kind,
        status=ResearchRunStatus.QUEUED.value,
        schema_version=source_manifest.schema_version,
        manifest_checksum="mc",
        manifest={},
        result=None,
        result_checksum=None,
        error_code=None,
        error_summary=None,
        # job_id 非 None:跳过双写 background_jobs(mock session 无真实 DB)。
        job_id="BJ-312-1",
        requested_by="agent:mcp",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        updated_at=datetime(2026, 9, 4, tzinfo=UTC),
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
        idempotency_key="idem-312-replay-mcp",
        requested_by="agent:mcp",
    )

    assert env.status == "ok"
    new_manifest = captured["manifest"]
    assert new_manifest.actor_type is ResearchActorType.AGENT
    assert new_manifest.requested_by == "agent:mcp"
    assert new_manifest.replay_of_run_id == source_manifest.run_id


# ---------------------------------------------------------------------------
# REST replay:默认 human 不变;显式 agent 放行;llm schema 层 422
# ---------------------------------------------------------------------------


@pytest.fixture
def rest_client() -> TestClient:
    app = FastAPI()
    app.include_router(research_runs_router)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    return TestClient(app)


def _patch_rest_store(
    monkeypatch: pytest.MonkeyPatch,
    source: ResearchRunRecord,
    *,
    captured: list[ResearchRunManifest],
) -> None:
    async def _fake_get(self: object, run_id: str) -> ResearchRunRecord:
        return source

    async def _fake_create_or_get(
        self: object, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]:
        captured.append(manifest)
        return _record(manifest, ResearchRunStatus.QUEUED), True

    row = ResearchRunModel(
        run_id="RR-312-rest-000001",
        idempotency_key="idem-312-rest",
        replay_of_run_id=source.manifest.run_id,
        strategy_id=source.manifest.strategy_spec.strategy_id,
        strategy_kind=source.manifest.strategy_kind,
        status=ResearchRunStatus.QUEUED.value,
        schema_version=source.manifest.schema_version,
        manifest_checksum="mc",
        manifest={},
        result=None,
        result_checksum=None,
        error_code=None,
        error_summary=None,
        # job_id 非 None:跳过双写 background_jobs(mock session 无真实 DB)。
        job_id="BJ-312-2",
        requested_by="user:312",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        updated_at=datetime(2026, 9, 4, tzinfo=UTC),
    )

    async def _fake_repo_get(self: object, run_id: str) -> ResearchRunModel:
        return row

    monkeypatch.setattr(SqlAlchemyResearchRunStore, "get", _fake_get)
    monkeypatch.setattr(
        SqlAlchemyResearchRunStore, "create_or_get", _fake_create_or_get
    )
    monkeypatch.setattr(ResearchRunRepository, "get", _fake_repo_get)


def test_rest_replay_defaults_to_human(
    rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_manifest = _manifest()
    source = _record(source_manifest, ResearchRunStatus.COMPLETED)
    captured: list[ResearchRunManifest] = []
    _patch_rest_store(monkeypatch, source, captured=captured)

    response = rest_client.post(
        f"/api/research/runs/{source_manifest.run_id}/replay",
        json={"idempotency_key": "idem-312-rest-human", "requested_by": "user:312"},
    )

    assert response.status_code == 201, response.text
    assert captured[0].actor_type is ResearchActorType.HUMAN


def test_rest_replay_explicit_agent(
    rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_manifest = _manifest()
    source = _record(source_manifest, ResearchRunStatus.COMPLETED)
    captured: list[ResearchRunManifest] = []
    _patch_rest_store(monkeypatch, source, captured=captured)

    response = rest_client.post(
        f"/api/research/runs/{source_manifest.run_id}/replay",
        json={
            "idempotency_key": "idem-312-rest-agent",
            "requested_by": "agent:mcp",
            "actor_type": "agent",
        },
    )

    assert response.status_code == 201, response.text
    assert captured[0].actor_type is ResearchActorType.AGENT


def test_rest_replay_rejects_llm_before_store_access(
    rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_manifest = _manifest()
    source = _record(source_manifest, ResearchRunStatus.COMPLETED)
    captured: list[ResearchRunManifest] = []
    _patch_rest_store(monkeypatch, source, captured=captured)

    response = rest_client.post(
        f"/api/research/runs/{source_manifest.run_id}/replay",
        json={
            "idempotency_key": "idem-312-rest-llm",
            "requested_by": "assistant",
            "actor_type": "llm",
        },
    )

    assert response.status_code == 422
    # schema 层即拒绝,未触达仓储。
    assert captured == []
