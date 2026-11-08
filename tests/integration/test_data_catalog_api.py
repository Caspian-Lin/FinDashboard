"""数据目录 REST 契约(issue #509):分页/详情/覆盖/引用/404。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.data_catalog import router
from finboard_data.releases import (
    ReleaseDatasetKind,
    ReleasedInstrument,
    ResearchDatasetRelease,
    default_execution_metadata,
)
from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    ListingStatus,
    Market,
)

pytestmark = pytest.mark.asyncio

_RELEASE_ID = "TEST-CATALOG-API"


def _release() -> ResearchDatasetRelease:
    instrument = ReleasedInstrument(
        code="APIT509.SH",
        name="接口目录样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 5, 17, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE, instrument_type=InstrumentType.STOCK
        ),
        artifact_path="daily/APIT509.SH.parquet",
        artifact_checksum="c" * 64,
        artifact_size=512,
        row_count=4,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        expected_sessions=4,
        missing_sessions=1,
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("0.75"),
        category="a_share_stock",
        ready=True,
        list_date=date(2020, 1, 1),
        status=ListingStatus.ACTIVE,
    )
    return ResearchDatasetRelease(
        release_id=_RELEASE_ID,
        dataset_name="test-catalog-api",
        source="tushare",
        version="v1",
        schema_version="v1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        period=BarPeriod.D1,
        adjustment="none",
        fields=("trade_date", "close"),
        availability_rules=(),
        code_version="test",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        instruments=(instrument,),
        capabilities=(),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={},
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        release_checksum="d" * 64,
    )


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await ResearchDatasetReleaseRepository(db_session).publish(_release())
    await db_session.commit()
    app = FastAPI()
    app.include_router(router)

    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_catalog_list_and_detail(client: AsyncClient) -> None:
    resp = await client.get("/api/data/catalog")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 10
    keys = {item["key"] for item in payload["items"]}
    assert "bars" in keys
    assert "daily_metrics" in keys
    daily = next(
        item for item in payload["items"] if item["key"] == "daily_metrics"
    )
    assert daily["release"]["release_id"] == _RELEASE_ID

    resp = await client.get("/api/data/catalog/datasets/daily_metrics")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["fields"]
    assert detail["releases"][0]["release_id"] == _RELEASE_ID


async def test_catalog_instruments_and_presence(client: AsyncClient) -> None:
    resp = await client.get(
        f"/api/data/catalog/releases/{_RELEASE_ID}/instruments?missing_only=true"
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["items"][0]["missing_sessions"] == 1

    resp = await client.get("/api/data/catalog/instruments/APIT509.SH")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp = await client.get(
        "/api/data/catalog/datasets/daily_metrics/coverage"
    )
    assert resp.status_code == 200
    coverage = resp.json()
    assert coverage["symbols_with_gaps"] == 1


async def test_catalog_unknown_release_404(client: AsyncClient) -> None:
    resp = await client.get("/api/data/catalog/releases/NOPE")
    assert resp.status_code == 404
    resp = await client.get(
        "/api/data/catalog/releases/NOPE/instruments"
    )
    assert resp.status_code == 404
