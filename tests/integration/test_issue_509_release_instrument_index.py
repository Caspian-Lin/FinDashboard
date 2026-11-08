"""逐标的发布目录索引:发布写入 + manifest 自愈 + 检索(issue #509)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.releases import (
    ReleaseDatasetKind,
    ReleasedInstrument,
    ResearchDatasetRelease,
    default_execution_metadata,
)
from finboard_persistence.catalog_repo import CatalogRepository
from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository
from finboard_persistence.models import ResearchDatasetReleaseInstrumentModel
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    DatasetQualityStatus,
    InstrumentType,
    ListingStatus,
    Market,
)

pytestmark = pytest.mark.asyncio

_RELEASE_ID = "TEST-CATALOG-509"
_START = date(2024, 1, 2)
_END = date(2024, 1, 5)


def _instrument(code: str, index: int) -> ReleasedInstrument:
    return ReleasedInstrument(
        code=code,
        name=f"目录样本{index}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 5, 17, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        artifact_path=f"daily/{code}.parquet",
        artifact_checksum="a" * 64,
        artifact_size=1024,
        row_count=4,
        start_date=_START,
        end_date=_END,
        expected_sessions=4,
        missing_sessions=index,  # 让第二个标的带缺口,验证 missing_only
        suspended_sessions=0,
        anomaly_count=0,
        coverage_pct=Decimal("1") - Decimal(index) / Decimal("10"),
        category="a_share_stock",
        ready=True,
        issues=(),
        list_date=date(2020, 1, 1),
        status=ListingStatus.ACTIVE,
    )


def _release() -> ResearchDatasetRelease:
    instruments = (
        _instrument("TST509.SH", 0),
        _instrument("TST510.SH", 1),
        _instrument("TST511.SH", 2),
    )
    return ResearchDatasetRelease(
        release_id=_RELEASE_ID,
        dataset_name="test-catalog",
        source="tushare",
        version="v1",
        schema_version="v1",
        start_date=_START,
        end_date=_END,
        period=BarPeriod.D1,
        adjustment="none",
        fields=("trade_date", "close"),
        availability_rules=(),
        code_version="test",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        instruments=instruments,
        capabilities=(),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={},
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        release_checksum="b" * 64,
    )


@pytest_asyncio.fixture
async def published(db_session: AsyncSession) -> ResearchDatasetRelease:
    release = _release()
    await ResearchDatasetReleaseRepository(db_session).publish(release)
    await db_session.commit()
    yield release
    await db_session.rollback()


async def test_publish_writes_instrument_index(
    db_session: AsyncSession, published: ResearchDatasetRelease
) -> None:
    repo = CatalogRepository(db_session)
    header = await repo.release_header(_RELEASE_ID)
    assert header is not None
    assert header.dataset_kind is ReleaseDatasetKind.DAILY_METRICS
    assert header.symbol_count == 3
    assert await repo.count_instruments(_RELEASE_ID) == 3

    rows, total = await repo.list_instruments(_RELEASE_ID, limit=2)
    assert total == 3
    assert [row.code for row in rows] == ["TST509.SH", "TST510.SH"]

    missing, missing_total = await repo.list_instruments(
        _RELEASE_ID, missing_only=True
    )
    assert missing_total == 2
    assert {row.code for row in missing} == {"TST510.SH", "TST511.SH"}

    presence, presence_total = await repo.find_presence("TST509.SH")
    assert presence_total == 1
    assert presence[0].release_id == _RELEASE_ID

    assert await repo.release_ids_covering(("TST509.SH", "TST510.SH")) == [
        _RELEASE_ID
    ]
    assert await repo.release_ids_covering(("TST509.SH", "NOPE.SH")) == []


async def test_ensure_rebuilds_from_manifest(
    db_session: AsyncSession, published: ResearchDatasetRelease
) -> None:
    await db_session.execute(
        delete(ResearchDatasetReleaseInstrumentModel).where(
            ResearchDatasetReleaseInstrumentModel.release_id == _RELEASE_ID
        )
    )
    await db_session.commit()
    repo = CatalogRepository(db_session)
    assert await repo.count_instruments(_RELEASE_ID) == 0
    assert await repo.ensure_instruments(_RELEASE_ID) is True
    assert await repo.count_instruments(_RELEASE_ID) == 3
    assert await repo.ensure_instruments(_RELEASE_ID) is True


async def test_release_header_excludes_manifest_but_keeps_kind(
    db_session: AsyncSession, published: ResearchDatasetRelease
) -> None:
    repo = CatalogRepository(db_session)
    rows, total = await repo.list_releases(kind=ReleaseDatasetKind.DAILY_METRICS)
    assert total == 1
    assert rows[0].release_id == _RELEASE_ID
    assert rows[0].coverage_pct > 0
    # 目录列表只投影列,不触发 manifest 反序列化(header 无 instruments 字段)。
    assert not hasattr(rows[0], "instruments")


async def test_release_references_empty(
    db_session: AsyncSession, published: ResearchDatasetRelease
) -> None:
    repo = CatalogRepository(db_session)
    assert await repo.list_references(_RELEASE_ID) == []
