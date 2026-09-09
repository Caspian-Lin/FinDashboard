"""平台预置因子端到端测试(issue #398,需 PostgreSQL)。

真实链路:bars 发布 + joint 研究发布登记 → **真实 FactorSeriesBuildExecutor**
(stub providers 进程内构建 + 真实前缀不变性审计)→ ``research_factor_series``
落库(kind=predefined_factor)→ 引用 ``p_return_21d`` 的已发布规格经
REST ``POST /api/research/runs`` 消费:

* multi_period + ``factor_series_ids`` 声明 → 201,manifest 冻结序列引用
  (capability = ``factor:p_return_21d``);
* multi_period 未声明(无已构建序列)→ 422 具名
  ``predefined_factor_series_coverage_missing`` + 重建命令;
* single_shot 未声明 → 422 具名 ``predefined_factor_series_undeclared``。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_backtest.background_jobs.executors.factor_series_build import (
    FactorSeriesBuildExecutor,
)
from finboard_backtest.factors.predefined import predefined_factor_commit
from finboard_backtest.research_run.contracts import manifest_from_json
from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec
from finboard_backtest.strategy_spec import (
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import RELEASE_FIELDS, ReleaseDatasetKind
from finboard_persistence import (
    FactorSeriesRecord,
    FactorSeriesRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
    session_factory,
)
from finboard_shared.types import BarPeriod, DatasetQualityStatus
from tests.integration.test_issue_355_snapshot_anchor_hint import _instrument
from tests.integration.test_research_run_signal_engine_worker import SYMBOLS
from tests.unit.research_sandbox.test_issue_398_predefined_channel import (
    _provider_factory,
)

pytestmark = pytest.mark.asyncio

P_FACTOR = "p_return_21d"


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()
P_FACTOR_BARE = "return_21d"
BASE_RELEASE = "p398-base-release"
JOINT_RELEASE = "p398-joint-release"
WINDOW_START = "2023-08-01"
WINDOW_END = "2023-09-30"


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
    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def _register_releases(session: AsyncSession, release_id: str) -> None:
    """登记 bars 主发布 + joint 研究发布(同名 dataset 派生,可并存)。"""
    release = ResearchDatasetRelease(
        release_id=release_id,
        dataset_name=f"{release_id}-bars",
        source="issue398",
        version="v1",
        schema_version="v1",
        start_date=date(2023, 8, 1),
        end_date=date(2023, 9, 30),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2023, 8, 1, tzinfo=UTC),
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
        release_checksum=_sha256(release_id),
    )
    joint = ResearchDatasetRelease(
        release_id=f"{release_id}-daily",
        dataset_name=f"{release_id}-daily-metrics",
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        source="issue398",
        version="v1",
        schema_version="v1",
        start_date=date(2023, 8, 1),
        end_date=date(2023, 9, 30),
        period=BarPeriod.D1,
        adjustment="none",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2023, 8, 1, tzinfo=UTC),
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
        storage_uri=f"{release_id}-daily",
        release_checksum=_sha256(f"{release_id}-daily"),
    )
    repo = ResearchDatasetReleaseRepository(session)
    await repo.publish(release)
    await repo.publish(joint)
    await session.commit()


def _p_factor_spec(strategy_id: str, release_id: str) -> Any:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id=strategy_id,
        dataset_release_ids=(release_id, f"{release_id}-daily"),
    )
    node = FeatureNode(
        node_id="p_alpha",
        label="预置动量",
        kind=FeatureKind.FACTOR,
        operator=FeatureOperator.IDENTITY,
        source=P_FACTOR,
    )
    nodes = (*spec.feature_graph.nodes, node)
    return spec.model_copy(
        update={
            "feature_graph": spec.feature_graph.model_copy(update={"nodes": nodes})
        }
    )


async def _publish_spec(session: AsyncSession, spec: Any) -> None:
    plan = compile_registered_strategy_spec(spec)
    repo = ResearchStrategySpecRepository(session)
    await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=plan.checksum,
        payload=spec.canonical_payload(),
    )
    await repo.publish(spec.strategy_id, 1, expected_version=1)
    await session.commit()


def _queue_payload(spec: Any, record: FactorSeriesRecord, key: str) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "strategy_id": spec.strategy_id,
        "strategy_version": 1,
        "dataset_release_ids": list(spec.validation_plan.dataset_release_ids),
        "factor_snapshot_ids": [],
        "factor_series_ids": [record.series_id],
        # multi_period:序列按决策日索引提供 p_ 因子观测。
        "parameters": {"rebalance_frequency": "monthly"},
        "code_version": "abcdef0123456789",
        "initial_capital": "200000",
        "requested_by": "integration-test",
    }


class _ExecutorSettings:
    research_sandbox_enabled = False  # predefined 不应被沙箱门拦截
    research_code_repo_path = "data_cache/research_code"
    research_code_max_files = 8
    research_code_max_file_bytes = 65536
    research_sandbox_workspace_root = "data_cache/research_sandbox"
    research_sandbox_max_nan_ratio = 0.5
    research_sandbox_min_coverage = 0.5


class TestBuildAndConsume:
    async def test_build_audit_persist_then_enqueue(
        self, engine: AsyncEngine, client: httpx.AsyncClient, tmp_path: Any
    ) -> None:
        """构建 → 审计 → 落库 → REST 入队消费(验收主链路)。"""
        async with session_factory(engine)() as session:
            await _register_releases(session, BASE_RELEASE)
            spec = _p_factor_spec("issue398_ok", BASE_RELEASE)
            await _publish_spec(session, spec)

        # ---- 真实执行器:进程内构建 + 真实审计 ----
        from finboard_backtest.research_sandbox import predefined_runner

        async def runner(spec: FactorSeriesRunSpec) -> Any:
            return await predefined_runner.run_predefined_factor_series(
                spec,
                settings=_ExecutorSettings(),
                release_provider_factory=_provider_factory,
                workspace_root=tmp_path,
            )

        async def audit(spec: Any, baseline: Any, *, truncate_at: date) -> Any:
            return await predefined_runner.default_predefined_prefix_audit(
                spec, baseline, truncate_at=truncate_at
            )

        executor = FactorSeriesBuildExecutor(
            session_maker=session_factory(engine),
            settings_factory=lambda: _ExecutorSettings(),
            predefined_runner=runner,
            predefined_prefix_audit=audit,
        )
        result = await executor.execute(
            cast(
                Any,
                _JobRecord(
                    {
                        "kind": "predefined_factor",
                        "name": P_FACTOR_BARE,
                        "release_id": BASE_RELEASE,
                        "dataset_release_ids": [f"{BASE_RELEASE}-daily"],
                        "window_start": WINDOW_START,
                        "window_end": WINDOW_END,
                    }
                ),
            ),
            cast(Any, _noop_progress),
        )
        assert result.status == "succeeded", result.error_summary
        series_id = result.result_ref
        assert series_id is not None
        assert series_id.startswith("FS-")

        async with session_factory(engine)() as session:
            record = await FactorSeriesRepository(session).get(series_id)
        assert record is not None
        assert record.kind == "predefined_factor"
        assert record.code_commit == predefined_factor_commit(P_FACTOR_BARE)
        assert record.dates
        assert any(
            value is not None
            for day in record.values.values()
            for value in day.values()
        )

        # ---- REST 入队消费(第二次构建走缓存命中,series_id 一致)----
        payload = _queue_payload(
            spec, record, "issue398-freeze-1"
        )
        response = await client.post("/api/research/runs", json=payload)
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        async with session_factory(engine)() as session:
            row = await ResearchRunRepository(session).get(run_id)
        assert row is not None
        manifest = manifest_from_json(row.manifest)
        assert len(manifest.factor_series) == 1
        ref = manifest.factor_series[0]
        assert ref.artifact_id == series_id
        assert ref.checksum == record.content_checksum
        assert any("factor:p_return_21d" in cap for cap in ref.capabilities)

    async def test_multi_period_without_series_rejected(
        self,
        engine: AsyncEngine,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """multi_period 引用 p_ 因子但未构建序列 → 422 覆盖缺失具名。"""
        async with session_factory(engine)() as session:
            await _register_releases(session, BASE_RELEASE)
            spec = _p_factor_spec("issue398_missing", BASE_RELEASE)
            await _publish_spec(session, spec)
        _patch_trading_days(monkeypatch)
        payload = _queue_payload(
            spec,
            _placeholder_series(),
            "issue398-missing-1",
        )
        # 声明一个不存在的序列 ID:p_ 因子不在 series_covered → 走覆盖检查。
        payload["factor_series_ids"] = []
        response = await client.post("/api/research/runs", json=payload)
        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        assert "predefined_factor_series_coverage_missing" in detail
        assert P_FACTOR in detail
        assert "finboard_factor_series_build" in detail

    async def test_single_shot_without_declaration_rejected(
        self,
        engine: AsyncEngine,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """single_shot 引用 p_ 因子未声明序列 → 422 具名(无快照路径)。"""
        async with session_factory(engine)() as session:
            await _register_releases(session, BASE_RELEASE)
            spec = _p_factor_spec("issue398_single", BASE_RELEASE)
            await _publish_spec(session, spec)
        _patch_trading_days(monkeypatch)
        payload = _queue_payload(
            spec,
            _placeholder_series(),
            "issue398-single-1",
        )
        payload["factor_series_ids"] = []
        del payload["parameters"]["rebalance_frequency"]
        response = await client.post("/api/research/runs", json=payload)
        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        assert "predefined_factor_series_undeclared" in detail
        assert P_FACTOR in detail


class _JobRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.job_id = "JOB-398-int"


async def _noop_progress(done: int, total: int, phase: str) -> None:
    del done, total, phase


def _patch_trading_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """合成交易日历(不入库不落盘;覆盖检查只用于决策日推导)。"""

    async def _fake(primary: Any) -> list[date]:
        del primary
        return [date(2023, 8, 1) + timedelta(days=i) for i in range(61)]

    monkeypatch.setattr(
        "finboard_api.routes.research_runs._enqueue_trading_days", _fake
    )


def _placeholder_series() -> FactorSeriesRecord:
    """占位记录(只为取 payload 形状;不入库)。"""
    return FactorSeriesRecord.build(
        code_artifact=P_FACTOR_BARE,
        code_commit=predefined_factor_commit(P_FACTOR_BARE),
        kind="predefined_factor",
        release_id=BASE_RELEASE,
        dataset_release_ids=(),
        params={},
        window_start=date(2023, 8, 1),
        window_end=date(2023, 9, 30),
        dates=[date(2023, 8, 1)],
        values={"2023-08-01": {"600000.SH": 0.01}},
    )


