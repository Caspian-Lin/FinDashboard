"""universe 预检入队快速失败集成测试(issue #186)。

覆盖验收:「对 list_date 全 null 的发布入队 min_listing_days>0 策略,秒级
失败且错误指向 list_date」—— 通过真实 REST 入队端点 ``POST /api/research/runs``
(与 MCP ``_build_queued_manifest`` 共用同一预检)验证:

* 发布 instruments 的 list_date 全 null 时,入队立即 422,错误信息含
  ``list_date`` 与 ``listing_age_below_minimum`` 排除统计,不产生 queued 行;
* 元数据齐备时入队成功(正对照),确保预检不误伤正常研究运行。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。只登记发布/策略/运行记录,
不连 broker / 不下单 / 不跑 worker 回测。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research_runs import router as research_runs_router
from finboard_backtest.strategy_spec import (
    ResearchStrategySpec,
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import (
    RELEASE_FIELDS,
    ReleasedInstrument,
    default_execution_metadata,
)
from finboard_persistence import (
    ResearchDatasetReleaseRepository,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
    session_factory,
)
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    Market,
)

pytestmark = pytest.mark.asyncio

RELEASE_ID = "integration-precheck-v1"


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """复用 conftest 建表 engine;每例测试自行清理本测试写入的记录。"""
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))
        await conn.execute(
            text(
                "DELETE FROM research_strategy_specs "
                "WHERE strategy_id LIKE 'integration_precheck%'"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM research_dataset_releases "
                "WHERE release_id LIKE 'integration-precheck%'"
            )
        )


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """最小 app:只挂 research_runs 路由,``get_db_session`` 用独立 session。"""
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


def _instrument(code: str, list_date: date | None) -> ReleasedInstrument:
    return ReleasedInstrument(
        code=code,
        name=code,
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
        list_date=list_date,
        delist_date=None,
        industry=None,
    )


def _release(symbols: tuple[tuple[str, date | None], ...]) -> ResearchDatasetRelease:
    return ResearchDatasetRelease(
        release_id=RELEASE_ID,
        dataset_name="integration_precheck_bars",
        source="integration-precheck",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=RELEASE_FIELDS,
        availability_rules=(("instrument_metadata", "available_at <= decision_at"),),
        code_version="abcdef0123456789",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
        instruments=tuple(_instrument(code, list_date) for code, list_date in symbols),
        capabilities=(
            AssetCapability(
                key="stock",
                status=CapabilityStatus.READY,
                symbol_count=len(symbols),
                ready_count=len(symbols),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1.0"},
        storage_uri=RELEASE_ID,
        release_checksum="b" * 64,
    )


async def _register_release(
    db_session: AsyncSession,
    symbols: tuple[tuple[str, date | None], ...],
) -> None:
    await ResearchDatasetReleaseRepository(db_session).publish(_release(symbols))
    await db_session.commit()


async def _register_published_spec(
    db_session: AsyncSession,
) -> ResearchStrategySpec:
    """multi_factor 模板:universe 默认 min_listing_days=60,无 factor 快照要求。"""
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="integration_precheck_strategy",
        dataset_release_ids=(RELEASE_ID,),
    )
    plan = compile_registered_strategy_spec(spec)
    repo = ResearchStrategySpecRepository(db_session)
    await repo.create_draft(
        strategy_id=spec.strategy_id,
        schema_version=spec.schema_version,
        name=spec.name,
        strategy_kind=spec.strategy_kind,
        checksum=plan.checksum,
        payload=spec.canonical_payload(),
    )
    await repo.publish(spec.strategy_id, 1, expected_version=1)
    await db_session.commit()
    return spec


def _queue_payload(spec: ResearchStrategySpec) -> dict[str, object]:
    # 多期回放路径不要求预建因子快照,让预检成为唯一拦截点。
    return {
        "idempotency_key": "integration-precheck-queue",
        "strategy_id": spec.strategy_id,
        "strategy_version": 1,
        "dataset_release_ids": [RELEASE_ID],
        "parameters": {"rebalance_frequency": "monthly"},
        "code_version": "abcdef0123456789",
        "initial_capital": "200000",
        "requested_by": "integration-test",
    }


async def test_enqueue_seconds_fail_when_list_date_all_null(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """list_date 全 null + min_listing_days>0:入队立即 422,错误指向 list_date。"""
    await _register_release(
        db_session,
        (
            ("600001.SH", None),
            ("600002.SH", None),
            ("600003.SH", None),
        ),
    )
    spec = await _register_published_spec(db_session)

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    # 根因字段名与排除统计都在响应里,而不是泛化报错。
    assert "list_date" in detail
    assert "listing_age_below_minimum=3" in detail
    assert "候选池为空" in detail
    # 快速失败:不产生 queued research_runs 行,不双写 background_jobs。
    rows = await ResearchRunRepository(db_session).list_recent(limit=10)
    assert all(row.strategy_kind != spec.strategy_kind for row in rows)


async def test_enqueue_succeeds_when_metadata_complete(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """元数据齐备(list_date 有值):预检放行,入队成功生成 queued 行(正对照)。

    同时也是 issue #203 的 multi_period 正对照:声明 rebalance_frequency 后
    不要求预建因子快照(价格因子按发布每期重算)。
    """
    await _register_release(
        db_session,
        (
            ("600001.SH", date(2020, 1, 1)),
            ("600002.SH", date(2020, 1, 1)),
            ("600003.SH", date(2020, 1, 1)),
        ),
    )
    spec = await _register_published_spec(db_session)

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["strategy_kind"] == "multi_factor"
    assert body["job_id"] is not None
    assert body["status"] == "queued"


async def test_enqueue_seconds_fail_when_single_shot_missing_snapshots(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """issue #203:未声明 rebalance_frequency(single_shot)且无快照:入队即 422。

    single_shot 的决策时点只能来自冻结因子快照 —— 缺快照秒级失败并附
    execution_mode 与缺失因子源,不再等执行期才报;multi_period 不受影响
    (见上例正对照)。
    """
    await _register_release(
        db_session,
        (
            ("600001.SH", date(2020, 1, 1)),
            ("600002.SH", date(2020, 1, 1)),
            ("600003.SH", date(2020, 1, 1)),
        ),
    )
    spec = await _register_published_spec(db_session)
    payload = _queue_payload(spec)
    # 去掉 rebalance_frequency → single_shot;未提供 factor_snapshot_ids。
    payload.pop("parameters")

    response = await client.post("/api/research/runs", json=payload)

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "execution_mode=single_shot" in detail
    assert "factor_snapshot_ids" in detail
    # 根因细分:指向缺失的因子源与修复路径(声明频率或冻结快照)。
    assert "rebalance_frequency=monthly|quarterly" in detail
    # 快速失败:不产生 queued research_runs 行,不双写 background_jobs。
    rows = await ResearchRunRepository(db_session).list_recent(limit=10)
    assert all(row.strategy_kind != spec.strategy_kind for row in rows)
