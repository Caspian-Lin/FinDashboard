"""instruments API 路由的单元测试(issue #58)。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finboard_data import AssetCapability, CapabilityStatus, ResearchDatasetRelease
from finboard_shared.types import BarPeriod, DatasetQualityStatus


def _dataset_release() -> ResearchDatasetRelease:
    return ResearchDatasetRelease(
        release_id="api-r77-v1",
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
        version="api-v1",
        schema_version="v1",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
        period=BarPeriod.D1,
        adjustment="qfq",
        fields=("timestamp", "close"),
        availability_rules=(("daily_bars", "T 日收盘后可知"),),
        code_version="deadbeef",
        published_at=datetime(2024, 1, 6, tzinfo=UTC),
        instruments=(),
        capabilities=(
            AssetCapability(
                key="futures",
                status=CapabilityStatus.INCOMPLETE,
                symbol_count=0,
                ready_count=0,
                missing_requirements=("no_instruments",),
            ),
        ),
        quality_status=DatasetQualityStatus.PASSED,
        quality_report={"release_coverage": "1"},
        storage_uri="api-r77-v1",
        release_checksum="a" * 64,
    )


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    return session


class TestInstrumentsList:
    @pytest.mark.asyncio
    async def test_list_instruments(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_instruments

        # 构造 mock result
        row = MagicMock()
        row.code = "600519.SH"
        row.name = "贵州茅台"
        row.market = "a_share"
        row.instrument_type = "stock"
        row.exchange = "SSE"
        row.list_date = date(2001, 8, 27)
        row.delist_date = None
        row.status = "active"
        row.sector = "白酒"
        row.industry = "食品饮料"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_instruments(
            market="a_share",
            instrument_type="stock",
            limit=100,
            session=mock_session,
        )
        assert len(items) == 1
        assert items[0].code == "600519.SH"
        assert items[0].instrument_type == "stock"

    @pytest.mark.asyncio
    async def test_get_instrument_not_found(self, mock_session: MagicMock) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import get_instrument

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await get_instrument("NOTEXIST.SH", session=mock_session)
        assert exc_info.value.status_code == 404


class TestDatasetManifestList:
    @pytest.mark.asyncio
    async def test_list_manifests(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_dataset_manifests

        row = MagicMock()
        row.id = 1
        row.dataset_name = "daily_bars"
        row.source = "akshare"
        row.version = "2024Q1"
        row.start_date = date(2024, 1, 1)
        row.end_date = date(2024, 6, 30)
        row.row_count = 10000
        row.symbol_count = 100
        row.coverage_pct = Decimal("0.98")
        row.gaps = []
        row.checksum = "abc123"
        row.quality_status = "passed"
        row.quality_report = {}
        row.published_at = date(2024, 7, 1)
        row.code_version = "v1"

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_dataset_manifests(
            dataset_name="daily_bars",
            quality_status="passed",
            limit=50,
            session=mock_session,
        )
        assert len(items) == 1
        assert items[0].dataset_name == "daily_bars"
        assert items[0].quality_status == "passed"


class TestResearchDatasetReleases:
    @pytest.mark.asyncio
    async def test_list_releases_exposes_capability_report(
        self,
        mock_session: MagicMock,
    ) -> None:
        from finboard_api.routes.instruments import list_dataset_releases

        row = MagicMock()
        row.manifest = _dataset_release().as_dict()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_dataset_releases(
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            quality_status="passed",
            limit=50,
            session=mock_session,
        )
        assert items[0].release_id == "api-r77-v1"
        assert items[0].capabilities[0].status == "incomplete"
        assert items[0].capabilities[0].missing_requirements == ["no_instruments"]

    @pytest.mark.asyncio
    async def test_get_release_not_found(self, mock_session: MagicMock) -> None:
        from fastapi import HTTPException

        from finboard_api.routes.instruments import get_dataset_release

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(HTTPException) as exc_info:
            await get_dataset_release("missing", session=mock_session)
        assert exc_info.value.status_code == 404


class TestLifecycleEvents:
    @pytest.mark.asyncio
    async def test_list_lifecycle(self, mock_session: MagicMock) -> None:
        from finboard_api.routes.instruments import list_lifecycle_events

        row = MagicMock()
        row.id = 1
        row.symbol = "600519.SH"
        row.event_type = "dividend"
        row.effective_date = date(2024, 6, 19)
        from datetime import datetime

        row.available_at = datetime(2024, 6, 19, 9, 0, tzinfo=UTC)
        row.source = "tushare"
        row.dataset_version = "2024Q2"
        row.details = {"per_share": "25.91"}

        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=result)

        items = await list_lifecycle_events("600519.SH", limit=100, session=mock_session)
        assert len(items) == 1
        assert items[0].event_type == "dividend"
