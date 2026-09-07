"""因子序列入队冻结与换发布守卫端到端测试(issue #360,需 PostgreSQL)。

真实链路(复用 #355 集成测试的种子设施):active 因子 artifact → bars
发布 → ``research_factor_series`` 序列落库 → 引用 u_ 因子的已发布规格:

* REST ``POST /api/research/runs`` + ``factor_series_ids``:匹配发布 →
  201,manifest 冻结 ``factor_series = [{series_id, content_checksum}]``
  且 input_checksum 覆盖序列引用(去掉序列引用的 input_checksum 不同);
  序列锚定其它发布 → 422 具名 ``series_release_mismatch``,附失效清单
  (FS- id / 因子名 / 锚定发布 / 窗口)与重建代价预估(N 条 x 预计分钟);
* MCP ``enqueue_research_run``:同一失配 invalid_argument,文案与 REST
  同源(共用 ``factor_series_rebuild_error``);
* REST ``POST /api/research/strategy-specs/validate``:同类失配给具名
  ``factor_series_anchor_warnings`` 提示(不阻断),全匹配为空列表。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_api.routes.strategy_specs import router as strategy_specs_router
from finboard_backtest.research_run.contracts import (
    manifest_from_json,
    to_json_value,
)
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import RELEASE_FIELDS
from finboard_mcp.execution import McpToolError
from finboard_mcp.tools import runs as run_tools
from finboard_persistence import (
    FactorSeriesRecord,
    FactorSeriesRepository,
    ResearchCodeArtifactRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunRepository,
    session_factory,
)
from finboard_shared.types import (
    BarPeriod,
    DatasetQualityStatus,
)
from tests.integration.test_issue_355_snapshot_anchor_hint import (
    _instrument,
    _publish_spec,
    _u_factor_spec,
)
from tests.integration.test_promotion_chain_closure import (
    FACTOR_COMMIT,
    FACTOR_NAME,
    U_FACTOR,
)
from tests.integration.test_research_run_signal_engine_worker import SYMBOLS

pytestmark = pytest.mark.asyncio

BASE_RELEASE = "series-base-release"
OTHER_RELEASE = "series-other-release"
WINDOW_START = "2024-01-01"
WINDOW_END = "2024-01-31"
DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 31)]


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in (
            "research_run_artifacts",
            "research_runs",
            "background_jobs",
            "research_trials",
            "research_experiments",
            "research_code_runs",
            "research_code_artifacts",
            "factor_feature_snapshots",
            "research_strategy_specs",
            "research_factor_series",
            "research_dataset_releases",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(research_runs_router)
    app.include_router(strategy_specs_router)
    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def _register_bars_release(session: AsyncSession, release_id: str) -> None:
    release = ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-bars",
        source="issue360",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=tuple(_instrument(code) for code in SYMBOLS),
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(SYMBOLS),
                ready_count=len(SYMBOLS),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=release_id,
        release_checksum=hashlib.sha256(release_id.encode()).hexdigest(),
    )
    await ResearchDatasetReleaseRepository(session).publish(release)
    await session.commit()


async def _register_active_artifact(session: AsyncSession) -> Any:
    return await ResearchCodeArtifactRepository(session).register(
        kind="factor",
        name=FACTOR_NAME,
        commit=FACTOR_COMMIT,
        path=f"factors/{FACTOR_NAME}",
        checksum="d" * 16,
        created_by="integration-test",
        status="active",
    )


async def _seed_series(engine: AsyncEngine, *, release_id: str) -> Any:
    """登记发布 + active artifact + 锚定 release_id 的序列,返回序列记录。"""
    async with session_factory(engine)() as session:
        await _register_bars_release(session, release_id)
        await _register_active_artifact(session)
        values = {
            day.isoformat(): {
                symbol: float(index + 1) for index, symbol in enumerate(SYMBOLS)
            }
            for day in DATES
        }
        record = FactorSeriesRecord.build(
            code_artifact=FACTOR_NAME,
            code_commit=FACTOR_COMMIT,
            kind="factor",
            release_id=release_id,
            dataset_release_ids=(),
            params={},
            window_start=date.fromisoformat(WINDOW_START),
            window_end=date.fromisoformat(WINDOW_END),
            dates=DATES,
            values=values,
            quality={"nan_ratio": 0.0, "coverage": 1.0},
            source_run_id=None,
        )
        await FactorSeriesRepository(session).upsert(record)
        await session.commit()
    return record


def _queue_payload(spec: Any, record: Any, key: str) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "strategy_id": spec.strategy_id,
        "strategy_version": 1,
        "dataset_release_ids": list(spec.validation_plan.dataset_release_ids),
        "factor_snapshot_ids": [],
        "factor_series_ids": [record.series_id],
        # multi_period:序列按决策日索引提供 u_ 因子观测(#360 的目标形态;
        # single_shot 决策时点仍只能来自冻结快照,零快照拒绝分支保持不变)。
        "parameters": {"rebalance_frequency": "monthly"},
        "code_version": "abcdef0123456789",
        "initial_capital": "200000",
        "requested_by": "integration-test",
    }


class TestRestEnqueueSeriesFreeze:
    async def test_match_freezes_series_into_manifest(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """匹配发布:201,manifest 冻结 series 引用且 input_checksum 覆盖。"""
        record = await _seed_series(engine, release_id=BASE_RELEASE)
        spec = _u_factor_spec("issue360_ok", BASE_RELEASE)
        async with session_factory(engine)() as session:
            await _publish_spec(session, spec)

        response = await client.post(
            "/api/research/runs",
            json=_queue_payload(spec, record, "issue360-freeze-1"),
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
        assert row is not None
        manifest = manifest_from_json(row.manifest)
        assert len(manifest.factor_series) == 1
        ref = manifest.factor_series[0]
        assert ref.artifact_id == record.series_id
        assert ref.checksum == record.content_checksum
        # input_checksum 覆盖序列引用:去掉序列引用(同 manifest 其余不变)
        # 的 input_checksum 必然不同。
        from dataclasses import replace

        without_series = replace(manifest, factor_series=())
        assert without_series.input_checksum != manifest.input_checksum
        # 序列化 payload 携带冻结引用(worker 端 manifest_from_json 可读回)。
        payload = to_json_value(manifest)
        assert isinstance(payload, dict)
        assert payload["factor_series"] == [
            {
                "artifact_id": record.series_id,
                "version": ref.version,
                "checksum": record.content_checksum,
                "capabilities": list(ref.capabilities),
            }
        ]

    async def test_mismatch_rejected_with_rebuild_estimate(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        """换 bars 主发布:422 具名 series_release_mismatch + 代价预估。"""
        record = await _seed_series(engine, release_id=BASE_RELEASE)
        # 目标发布(规格与 dataset_release_ids 用 OTHER_RELEASE)。
        async with session_factory(engine)() as session:
            await _register_bars_release(session, OTHER_RELEASE)
            spec = _u_factor_spec("issue360_new", OTHER_RELEASE)
            await _publish_spec(session, spec)

        spec = _u_factor_spec("issue360_new", OTHER_RELEASE)
        response = await client.post(
            "/api/research/runs",
            json=_queue_payload(
                spec, record, "issue360-mismatch-1"
            ),
        )
        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        assert "series_release_mismatch" in detail
        assert record.series_id in detail
        assert U_FACTOR in detail
        assert BASE_RELEASE in detail
        assert "预计 2 分钟" in detail  # 1 条 x 2 分钟/条
        assert "finboard_factor_series_build" in detail
        # 快速失败:不产生 queued 行。
        async with session_factory(engine)() as session:
            rows = await ResearchRunRepository(session).list_recent(limit=10)
        assert all(row.strategy_id != spec.strategy_id for row in rows)

    async def test_missing_series_rejected(self, engine: AsyncEngine, client: httpx.AsyncClient) -> None:
        record = await _seed_series(engine, release_id=BASE_RELEASE)
        spec = _u_factor_spec("issue360_missing", BASE_RELEASE)
        async with session_factory(engine)() as session:
            await _publish_spec(session, spec)
        payload = _queue_payload(spec, record, "issue360-missing-1")
        payload["factor_series_ids"] = ["FS-nonexistent000"]
        response = await client.post("/api/research/runs", json=payload)
        assert response.status_code == 422
        assert "因子序列不存在" in str(response.json()["detail"])


class TestMcpEnqueueSameGate:
    async def test_mcp_rejects_with_same_named_message(
        self, engine: AsyncEngine
    ) -> None:
        """MCP 通道同一失配:invalid_argument,文案与 REST 同源。"""
        record = await _seed_series(engine, release_id=BASE_RELEASE)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, OTHER_RELEASE)
            spec = _u_factor_spec("issue360_mcp", OTHER_RELEASE)
            await _publish_spec(session, spec)

        spec = _u_factor_spec("issue360_mcp", OTHER_RELEASE)
        app = _mcp_app(engine)
        payload = _queue_payload(
            spec, record, "issue360-mcp-1"
        )
        body = run_tools.parse_queue_payload(payload)
        with pytest.raises(McpToolError) as exc_info:
            await run_tools.enqueue_research_run(app, body)
        assert exc_info.value.kind == "invalid_argument"
        message = exc_info.value.message
        assert "series_release_mismatch" in message
        assert "预计 2 分钟" in message


class TestValidateChannelWarnings:
    async def test_validate_warns_on_mismatch_and_silent_on_match(
        self, engine: AsyncEngine, client: httpx.AsyncClient
    ) -> None:
        record = await _seed_series(engine, release_id=BASE_RELEASE)
        async with session_factory(engine)() as session:
            await _register_bars_release(session, OTHER_RELEASE)

        # 失配:validate 只见规格 + dataset_release_ids(不含序列锚定发布)。
        spec = _u_factor_spec("issue360_warn", OTHER_RELEASE)
        response = await client.post(
            "/api/research/strategy-specs/validate",
            json={"spec": spec.canonical_payload()},
        )
        assert response.status_code == 200, response.text
        warnings = response.json()["factor_series_anchor_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["code"] == "factor_series_anchor_mismatch"
        assert warnings[0]["factor_name"] == U_FACTOR
        assert warnings[0]["anchored_release_id"] == BASE_RELEASE
        assert "finboard_factor_series_build" in warnings[0]["message"]

        # 全匹配:validate 对 BASE_RELEASE 规格零噪音。
        spec_ok = _u_factor_spec("issue360_warn_ok", BASE_RELEASE)
        response = await client.post(
            "/api/research/strategy-specs/validate",
            json={"spec": spec_ok.canonical_payload()},
        )
        assert response.status_code == 200
        assert response.json()["factor_series_anchor_warnings"] == []
        assert record.series_id  # 序列仍在(校验语义不删除数据)


def _mcp_app(engine: AsyncEngine) -> Any:
    """构造 enqueue_research_run 需要的最小 McpAppContext(对齐 #355 测试)。"""
    from unittest.mock import MagicMock

    from finboard_app.config import Settings
    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.context import McpAppContext

    smaker = session_factory(engine)
    return McpAppContext(
        settings=Settings(
            research_sandbox_enabled=False,
            research_sandbox_workspace_root="unused",
        ),
        session_maker=smaker,
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=MagicMock(),
    )
