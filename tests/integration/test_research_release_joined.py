"""联合发布 → 因子快照 → 规格校验全链路集成测试(issue #187)。

用真实 PostgreSQL 注入 ``research_daily_metrics`` / ``research_financial_indicators``
行,经 ``ResearchDatasetReleaseService`` 冻结发布 bars(主) + daily_metrics +
financial_indicators 三份 release,再断言:

* 联合发布因子快照可产出(pb / 市值 / 换手 / ROE / 毛利率 + 价格因子);
* multi_factor 策略规格引用三份 release 通过 ``strategy_validate``,
  universe_precheck 落在 bars 主发布。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。只登记发布/策略/快照记录,
不连 broker / 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.research import router as research_router
from finboard_api.routes.strategy_specs import router as strategy_specs_router
from finboard_backtest.strategy_spec import (
    build_strategy_template,
    compile_registered_strategy_spec,
)
from finboard_data import FrozenReleaseProvider
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    FINANCIAL_INDICATORS_FIELDS,
    DatasetReleaseSpec,
    ReleaseDatasetKind,
)
from finboard_persistence import (
    InstrumentModel,
    ResearchDailyMetricModel,
    ResearchDatasetReleaseRepository,
    ResearchDatasetReleaseService,
    ResearchFinancialIndicatorModel,
    ResearchStrategySpecRepository,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_RELEASE_PREFIX = "integration-r187"
_START = date(2024, 1, 2)
# 终点取季度末:financial_indicators 按 report_period 对齐 quarter-end,
# 3/31 落在范围内;3/30~3/31 为周末,不影响 daily/bars 覆盖。
_END = date(2024, 3, 31)
_CODE = "600001.SH"


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """复用 conftest 建表 engine;每例测试自行清理本测试写入的记录。"""
    yield _engine


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(f"DELETE FROM research_dataset_releases WHERE release_id LIKE '{_RELEASE_PREFIX}%'")
        )
        await conn.execute(
            text(
                "DELETE FROM factor_feature_snapshots "
                f"WHERE dataset_release_id LIKE '{_RELEASE_PREFIX}%'"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM research_strategy_specs "
                f"WHERE strategy_id LIKE '{_RELEASE_PREFIX}%'"
            )
        )
        await conn.execute(
            text(f"DELETE FROM research_daily_metrics WHERE symbol = '{_CODE}'")
        )
        await conn.execute(
            text(f"DELETE FROM research_financial_indicators WHERE symbol = '{_CODE}'")
        )
        await conn.execute(
            text("DELETE FROM research_sync_batches WHERE dataset_version = 'r187-integration-v1'")
        )
        await conn.execute(
            text(f"DELETE FROM instruments WHERE code = '{_CODE}'")
        )


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """最小 app:挂 research + strategy-specs 路由,``get_db_session`` 独立 session。"""
    smaker = session_factory(engine)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with smaker() as session:
            yield session

    app = FastAPI()
    app.include_router(research_router)
    app.include_router(strategy_specs_router)
    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


def _trade_days() -> list[date]:
    result: list[date] = []
    current = _START
    while current <= _END:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


async def _seed_environment(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """instrument 行 + bars cache + research_* 行,构成真实发布输入。"""
    from finboard_persistence import ResearchSyncBatchModel

    db_session.add(
        InstrumentModel(
            code=_CODE,
            name="issue187 联合发布样本",
            market="a_share",
            instrument_type="stock",
            exchange="SSE",
            list_date=date(2020, 1, 1),
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    cache = ParquetCache(tmp_path / "cache")
    symbol = Symbol(_CODE, Market.A_SHARE)
    close = Decimal("10.0")
    bars = [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("10000"),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in _trade_days()
    ]
    await cache.write(symbol, BarPeriod.D1, "qfq", bars)

    batch = ResearchSyncBatchModel(
        dataset="daily_metrics",
        source="tushare",
        dataset_version="r187-integration-v1",
        code_version="test",
        status="published",
        quality_status="passed",
        expected_rows=2,
        received_rows=2,
        accepted_rows=2,
    )
    db_session.add(batch)
    await db_session.flush()

    for trade_date in _trade_days():
        db_session.add(
            ResearchDailyMetricModel(
                batch_id=batch.id,
                source="tushare",
                dataset_version="r187-integration-v1",
                symbol=_CODE,
                trade_date=trade_date,
                close=Decimal("10"),
                turnover_rate=Decimal("0.02"),
                turnover_rate_free=Decimal("0.018"),
                volume_ratio=Decimal("1.2"),
                pe=Decimal("10"),
                pe_ttm=Decimal("9.6"),
                pb=Decimal("9.5"),
                ps=Decimal("1.5"),
                ps_ttm=Decimal("1.4"),
                dividend_yield=Decimal("0.03"),
                dividend_yield_ttm=Decimal("0.031"),
                total_shares=Decimal("1000000000"),
                float_shares=Decimal("800000000"),
                free_shares=Decimal("750000000"),
                total_market_cap=Decimal("10000000000"),
                circulating_market_cap=Decimal("8000000000"),
                limit_status=0,
                observed_at=datetime.combine(
                    trade_date, datetime.min.time(), tzinfo=UTC
                ),
                available_at=datetime.combine(
                    trade_date, datetime.min.time(), tzinfo=UTC
                ),
            )
        )

    db_session.add(
        ResearchFinancialIndicatorModel(
            batch_id=batch.id,
            source="tushare",
            dataset_version="r187-integration-v1",
            symbol=_CODE,
            announcement_date=date(2024, 3, 29),
            report_period=date(2024, 3, 31),
            update_flag="1",
            eps=Decimal("1.0"),
            diluted_eps=Decimal("0.9"),
            book_value_per_share=Decimal("10"),
            operating_cash_flow_per_share=Decimal("1.2"),
            return_on_equity=Decimal("0.10"),
            weighted_return_on_equity=Decimal("0.11"),
            gross_profit_margin=Decimal("0.30"),
            net_profit_margin=Decimal("0.12"),
            debt_to_assets=Decimal("0.5"),
            revenue_yoy=Decimal("0.20"),
            net_profit_yoy=Decimal("0.15"),
            operating_cash_flow_yoy=Decimal("0.10"),
            observed_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
            available_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
        )
    )
    await db_session.commit()


async def _publish_releases(
    db_session: AsyncSession,
    tmp_path: Path,
) -> list[str]:
    service = ResearchDatasetReleaseService(
        db_session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    specs = (
        DatasetReleaseSpec(
            release_id=f"{_RELEASE_PREFIX}-bars",
            dataset_name="r187_daily_bars",
            source="fixed_sample",
            version="v1",
            start_date=_START,
            end_date=_END,
            code_version="test",
            required_capabilities=("stock",),
        ),
        DatasetReleaseSpec(
            release_id=f"{_RELEASE_PREFIX}-daily",
            dataset_name="r187_daily_metrics",
            source="tushare",
            version="v1",
            start_date=_START,
            end_date=_END,
            code_version="test",
            fields=DAILY_METRICS_FIELDS,
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
            required_capabilities=("stock",),
            adjustment="none",
        ),
        DatasetReleaseSpec(
            release_id=f"{_RELEASE_PREFIX}-fina",
            dataset_name="r187_financial_indicators",
            source="tushare",
            version="v1",
            start_date=_START,
            end_date=_END,
            code_version="test",
            fields=FINANCIAL_INDICATORS_FIELDS,
            dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
            required_capabilities=("stock",),
            adjustment="none",
        ),
    )
    for spec in specs:
        await service.publish(spec, [_CODE])
    await db_session.commit()
    return [spec.release_id for spec in specs]


async def test_joined_releases_build_cross_section_snapshot(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """三份 release 联合经 REST 快照端点产出基本面 + 价格因子观测。"""
    await _seed_environment(db_session, tmp_path)
    bars_id, daily_id, fina_id = await _publish_releases(db_session, tmp_path)

    from finboard_backtest.factor_lab import build_cross_section_feature_snapshot_from_releases

    repo = ResearchDatasetReleaseRepository(db_session)
    releases = [
        await repo.require_usable(release_id)
        for release_id in (bars_id, daily_id, fina_id)
    ]
    providers = {
        release.release_id: FrozenReleaseProvider(
            release_root=tmp_path / "releases",
            release_id=release.release_id,
        )
        for release in releases
    }
    snapshot = await build_cross_section_feature_snapshot_from_releases(
        releases=releases,
        providers=providers,
        decision_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
        code_version="test",
        momentum_lookback=5,
        volatility_windows=(20,),
    )

    observed = {obs.feature_name: obs for obs in snapshot.observations}
    assert observed["pb"].value == 9.5
    assert observed["market_cap"].value == 10_000_000_000.0
    assert observed["turnover_rate"].value == 0.02
    assert observed["roe"].value == 0.10
    assert observed["gross_profit_margin"].value == 0.30
    assert observed["momentum"].symbol == _CODE
    assert observed["volatility_20d"].symbol == _CODE


async def test_multi_factor_spec_references_joined_releases_passes_validate(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """multi_factor 规格引用三份 release 通过 strategy_validate,universe 预检落 bars 主发布。"""
    await _seed_environment(db_session, tmp_path)
    bars_id, daily_id, fina_id = await _publish_releases(db_session, tmp_path)

    spec = build_strategy_template(
        "multi_factor",
        strategy_id=f"{_RELEASE_PREFIX}_strategy",
        dataset_release_ids=(bars_id, daily_id, fina_id),
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

    response = await client.post(
        "/api/research/strategy-specs/validate",
        json={"spec": spec.canonical_payload()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert set(body["dataset_release_ids"]) == {bars_id, daily_id, fina_id}
    assert body["universe_precheck"] is not None
    assert body["universe_precheck"]["total_candidates"] == 1
    # 研究数据发布不进入候选池,候选池评估落在 bars 主发布。
    assert body["universe_precheck"]["included"] == 1


async def test_daily_release_reads_pit_filtered_from_db(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """daily_metrics 发布冻结全部交易日记录,PIT 门控读取按 available_at 过滤。"""
    await _seed_environment(db_session, tmp_path)
    _bars_id, daily_id, _fina_id = await _publish_releases(db_session, tmp_path)

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=daily_id,
    )
    released = await provider.fetch_daily_metrics(
        Symbol(_CODE, Market.A_SHARE),
        start=_START,
        end=_END,
        decision_at=datetime(2024, 3, 31, 16, 0, tzinfo=UTC),
    )
    # 注入的是 2024-01-02~2024-03-31 全部交易日(64 条周末去除后的记录)。
    assert len(released) == len(_trade_days())
    assert released[-1].pb == Decimal("9.5")
    assert provider.release.dataset_kind is ReleaseDatasetKind.DAILY_METRICS
    assert provider.release.coverage_pct == Decimal("1")
