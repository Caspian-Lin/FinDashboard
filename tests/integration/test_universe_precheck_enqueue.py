"""universe 预检入队快速失败集成测试(issue #186;#203/#253 增补)。

覆盖验收:「对 list_date 全 null 的发布入队 min_listing_days>0 策略,秒级
失败且错误指向 list_date」—— 通过真实 REST 入队端点 ``POST /api/research/runs``
(与 MCP ``_build_queued_manifest`` 共用同一预检)验证:

* 发布 instruments 的 list_date 全 null 时,入队立即 422,错误信息含
  ``list_date`` 与 ``listing_age_below_minimum`` 排除统计,不产生 queued 行;
* 元数据齐备时入队成功(正对照),确保预检不误伤正常研究运行;
* issue #203:未声明频率(single_shot)缺快照秒级拒绝;
* issue #253:multi_period 声明财务因子(pb / roe)但未附加对应研究数据
  发布时,入队秒级具名拒绝(此前拖到执行期才报「identity 节点缺少数据源」),
  附加齐备后放行;
* issue #303:静态候选池 < ceil(1/生效 max_risk_contribution)(默认 0.35
  隐含买入池 >=3)时入队秒级拒绝,portfolio_config.overrides 调高阈值放行;
  阈值非法值与 risk_config.overrides 形态错误同样入队即拒。

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
    FeatureGraph,
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    RELEASE_FIELDS,
    ReleaseDatasetKind,
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


def _instrument(
    code: str,
    list_date: date | None,
    *,
    name: str | None = None,
    name_history: tuple[tuple[str, date, date | None], ...] = (),
) -> ReleasedInstrument:
    return ReleasedInstrument(
        code=code,
        name=name if name is not None else code,
        name_history=name_history,
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


def _release(
    symbols: tuple[tuple[str, date | None], ...],
    *,
    instruments: tuple[ReleasedInstrument, ...] | None = None,
) -> ResearchDatasetRelease:
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
        instruments=instruments
        if instruments is not None
        else tuple(_instrument(code, list_date) for code, list_date in symbols),
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
    *,
    instruments: tuple[ReleasedInstrument, ...] | None = None,
) -> None:
    await ResearchDatasetReleaseRepository(db_session).publish(
        _release(symbols, instruments=instruments)
    )
    await db_session.commit()


async def _register_daily_metrics_release(
    db_session: AsyncSession,
    symbols: tuple[tuple[str, date | None], ...],
) -> str:
    """注册最小 daily_metrics 研究数据发布(issue #187 / #213 预检路径)。"""
    release = ResearchDatasetRelease(
        release_id=f"{RELEASE_ID}-daily",
        dataset_name="integration_precheck_daily_metrics",
        source="integration-precheck",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        period=BarPeriod.D1,
        adjustment="none",
        fields=DAILY_METRICS_FIELDS,
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
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        storage_uri=f"{RELEASE_ID}-daily",
        release_checksum="c" * 64,
    )
    await ResearchDatasetReleaseRepository(db_session).publish(release)
    await db_session.commit()
    return release.release_id


async def _register_published_spec(
    db_session: AsyncSession,
    *,
    universe_updates: dict[str, object] | None = None,
    dataset_release_ids: tuple[str, ...] = (RELEASE_ID,),
    feature_graph: FeatureGraph | None = None,
) -> ResearchStrategySpec:
    """multi_factor 模板:universe 默认 min_listing_days=60,无 factor 快照要求。

    ``feature_graph`` 可替换模板特征图:multi_factor 模板引用 pb(daily_metrics
    派生,issue #253),只关心 universe 元数据诊断的用例用纯价格特征图避免
    特征可用性门控先拦。
    """
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="integration_precheck_strategy",
        dataset_release_ids=dataset_release_ids,
    )
    if feature_graph is not None:
        spec = spec.model_copy(update={"feature_graph": feature_graph})
    if universe_updates:
        spec = spec.model_copy(
            update={"universe": spec.universe.model_copy(update=universe_updates)}
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


def _price_only_feature_graph() -> FeatureGraph:
    """仅价格重算特征的模板变体(momentum / volatility_20d,issue #253)。"""
    return FeatureGraph(
        nodes=(
            FeatureNode(
                node_id="momentum",
                label="动量",
                kind=FeatureKind.FACTOR,
                operator=FeatureOperator.IDENTITY,
                source="momentum",
            ),
            FeatureNode(
                node_id="volatility",
                label="20 日波动率",
                kind=FeatureKind.FACTOR,
                operator=FeatureOperator.IDENTITY,
                source="volatility_20d",
            ),
            FeatureNode(
                node_id="composite",
                label="复合得分",
                kind=FeatureKind.COMPOSITE,
                operator=FeatureOperator.WEIGHTED_SUM,
                inputs=("momentum", "volatility"),
                weights=(0.5, 0.5),
            ),
        ),
        outputs=("composite",),
    )


def _queue_payload(spec: ResearchStrategySpec) -> dict[str, object]:
    # 多期回放路径不要求预建因子快照,让预检成为唯一拦截点。
    return {
        "idempotency_key": "integration-precheck-queue",
        "strategy_id": spec.strategy_id,
        "strategy_version": 1,
        "dataset_release_ids": list(spec.validation_plan.dataset_release_ids),
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
    spec = await _register_published_spec(
        db_session,
        feature_graph=_price_only_feature_graph(),
    )

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
    不要求预建因子快照;issue #253 起 multi_factor 模板引用的 pb 需要附加
    daily_metrics 研究发布(特征可用性门控),不再只挂 bars 主发布。
    """
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
    )

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


# ---- issue #213:市值过滤与 ST 真实判定的入队预检 ----


async def test_enqueue_seconds_fail_when_market_cap_unavailable(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """min_market_cap 声明但 market_cap 特征无数据源(未附 daily_metrics 发布
    且无快照观测):入队秒级 422,错误指向 market_cap。"""
    await _register_release(
        db_session,
        (
            ("600001.SH", date(2020, 1, 1)),
            ("600002.SH", date(2020, 1, 1)),
            ("600003.SH", date(2020, 1, 1)),
        ),
    )
    spec = await _register_published_spec(
        db_session,
        universe_updates={"min_market_cap": 1e10},
        feature_graph=_price_only_feature_graph(),
    )

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "market_cap" in detail
    assert "missing_market_cap=3" in detail
    assert "候选池为空" in detail


async def test_enqueue_succeeds_with_daily_metrics_release_attached(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """附 daily_metrics 研究发布时 market_cap 运行时可派生:预检不误报空池。"""
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        universe_updates={"min_market_cap": 1e10},
        dataset_release_ids=(RELEASE_ID, daily_id),
    )

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"


async def test_enqueue_seconds_fail_when_all_st_by_names(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """全池 ST(名称含 ST):静态预检按名称排除全部标的,秒级 422 指向 st_security。"""
    await _register_release(
        db_session,
        (
            ("600001.SH", date(2020, 1, 1)),
            ("600002.SH", date(2020, 1, 1)),
            ("600003.SH", date(2020, 1, 1)),
        ),
        instruments=tuple(
            _instrument(
                code,
                date(2020, 1, 1),
                name=f"ST样本{index}",
            )
            for index, code in enumerate(("600001.SH", "600002.SH", "600003.SH"))
        ),
    )
    spec = await _register_published_spec(
        db_session,
        feature_graph=_price_only_feature_graph(),
    )

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "st_security=3" in detail
    assert "候选池为空" in detail


# ---- issue #253:multi_period 特征可用性入队门控 ----


async def test_enqueue_seconds_fail_when_financial_factor_without_research_release(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """multi_factor 模板引用 pb 但只挂 bars 发布:入队秒级 422 且文案具名。

    multi_factor 模板的特征图声明 pb identity 源 —— multi_period 下 pb 只能
    来自 daily_metrics 研究数据发布;此前该组合入队成功、worker 开跑后才报
    「identity 节点缺少数据源: pb」,现在入队期直接具名拒绝。
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

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "execution_mode=multi_period" in detail
    assert "pb" in detail
    assert "daily_metrics" in detail
    assert "identity 节点缺少数据源" in detail
    # 快速失败:不产生 queued research_runs 行。
    rows = await ResearchRunRepository(db_session).list_recent(limit=10)
    assert all(row.strategy_kind != spec.strategy_kind for row in rows)


async def test_enqueue_seconds_fail_when_roe_without_financial_release(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """引用 roe 但只挂 bars + daily_metrics:具名指向 financial_indicators。"""
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
        feature_graph=FeatureGraph(
            nodes=(
                FeatureNode(
                    node_id="pb",
                    label="ROE",
                    kind=FeatureKind.FACTOR,
                    operator=FeatureOperator.IDENTITY,
                    source="roe",
                ),
                FeatureNode(
                    node_id="pb_rank",
                    label="ROE 排名",
                    kind=FeatureKind.TRANSFORM,
                    operator=FeatureOperator.CROSS_SECTION_RANK,
                    inputs=("pb",),
                ),
                FeatureNode(
                    node_id="value_score",
                    label="质量得分",
                    kind=FeatureKind.TRANSFORM,
                    operator=FeatureOperator.NEGATE,
                    inputs=("pb_rank",),
                ),
                FeatureNode(
                    node_id="momentum",
                    label="动量",
                    kind=FeatureKind.FACTOR,
                    operator=FeatureOperator.IDENTITY,
                    source="momentum",
                ),
                FeatureNode(
                    node_id="volatility",
                    label="20 日波动率",
                    kind=FeatureKind.FACTOR,
                    operator=FeatureOperator.IDENTITY,
                    source="volatility_20d",
                ),
                FeatureNode(
                    node_id="low_risk_score",
                    label="低风险得分",
                    kind=FeatureKind.TRANSFORM,
                    operator=FeatureOperator.NEGATE,
                    inputs=("volatility",),
                ),
                FeatureNode(
                    node_id="composite",
                    label="复合得分",
                    kind=FeatureKind.COMPOSITE,
                    operator=FeatureOperator.WEIGHTED_SUM,
                    inputs=("value_score", "momentum", "low_risk_score"),
                    weights=(0.3, 0.4, 0.3),
                ),
            ),
            outputs=("composite",),
        ),
    )

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "roe" in detail
    assert "financial_indicators" in detail


# ---- issue #303:组合硬约束配置传导与入队组合可行性预检 ----


async def test_enqueue_seconds_fail_when_static_pool_below_risk_cap_floor(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """静态池 2 只 + 默认 max_risk_contribution=0.35:入队秒级 422。

    默认 0.35 隐含买入池 >= ceil(1/0.35)=3 只,候选只有 2 只时组合阶段
    RiskBudgetError 数学不可行会让整个 run 执行完才 REJECTED —— 现在入队期
    直接拒绝,错误附生效阈值、ceil 推导与 portfolio_config 键位修复路径。
    """
    await _register_release(
        db_session,
        (
            ("600001.SH", date(2020, 1, 1)),
            ("600002.SH", date(2020, 1, 1)),
        ),
    )
    spec = await _register_published_spec(
        db_session,
        feature_graph=_price_only_feature_graph(),
    )

    response = await client.post("/api/research/runs", json=_queue_payload(spec))

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "ceil(1/0.35)=3" in detail
    assert "静态候选池仅 2 只" in detail
    assert "portfolio_config.overrides['max_risk_contribution']" in detail
    # 快速失败:不产生 queued research_runs 行。
    rows = await ResearchRunRepository(db_session).list_recent(limit=10)
    assert all(row.strategy_kind != spec.strategy_kind for row in rows)


async def test_enqueue_succeeds_when_portfolio_override_relaxes_risk_cap(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """portfolio_config.overrides 调高 max_risk_contribution 到 0.5:同一 2 只池放行。"""
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
    )
    payload = _queue_payload(spec)
    payload["portfolio_config"] = {"max_risk_contribution": 0.5}

    response = await client.post("/api/research/runs", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"


async def test_enqueue_seconds_fail_when_max_risk_contribution_invalid(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """阈值非法值(0 越界)入队即拒,错误附合法域。"""
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
    )
    payload = _queue_payload(spec)
    payload["portfolio_config"] = {"max_risk_contribution": 0}

    response = await client.post("/api/research/runs", json=payload)

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "portfolio_config.overrides['max_risk_contribution']" in detail
    assert "0 < 值 <= 1" in detail


async def test_enqueue_seconds_fail_when_risk_config_overrides_invalid(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """risk_config.overrides 形态非法(未知 rule_type)入队秒级 422。

    此前 risk_config 是死分区,任何形态错误都到执行期组合阶段才暴露。
    """
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
    )
    payload = _queue_payload(spec)
    payload["risk_config"] = {"rules": [{"rule_type": "no_such_rule"}]}

    response = await client.post("/api/research/runs", json=payload)

    assert response.status_code == 422, response.text
    detail = str(response.json()["detail"])
    assert "risk_config.overrides" in detail


async def test_enqueue_accepts_valid_risk_config_stop_loss_override(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    """合法 stop-loss 覆盖(risk_config)+ 3 只池(满足默认 0.35 门槛)放行。"""
    symbols = (
        ("600001.SH", date(2020, 1, 1)),
        ("600002.SH", date(2020, 1, 1)),
        ("600003.SH", date(2020, 1, 1)),
    )
    await _register_release(db_session, symbols)
    daily_id = await _register_daily_metrics_release(db_session, symbols)
    spec = await _register_published_spec(
        db_session,
        dataset_release_ids=(RELEASE_ID, daily_id),
    )
    payload = _queue_payload(spec)
    payload["risk_config"] = {
        "rules": [
            {
                "rule_type": "price_stop_loss",
                "enabled": True,
                "threshold": 0.08,
            }
        ]
    }

    response = await client.post("/api/research/runs", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
