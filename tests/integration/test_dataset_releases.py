"""研究数据集文件发布 + PostgreSQL 不可变登记集成测试(issue #77)。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_data import DatasetReleaseSpec, ImmutableReleaseError
from finboard_data.cache import ParquetCache
from finboard_persistence import (
    InstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchDatasetReleaseRepository,
    ResearchDatasetReleaseService,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_SYMBOLS = [
    "TST077.SH",
    "510300.SH",
    "513100.SH",
    "518880.SH",
    "511010.SH",
]


@pytest_asyncio.fixture
async def test_stock(db_session: AsyncSession) -> AsyncIterator[None]:
    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "TST077.SH")
    )
    db_session.add(
        InstrumentModel(
            code="TST077.SH",
            name="issue77 固定股票样本",
            market="a_share",
            instrument_type="stock",
            exchange="SSE",
            list_date=date(2020, 1, 1),
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.flush()
    yield
    await db_session.rollback()
    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "TST077.SH")
    )
    await db_session.commit()


async def _seed_bars(cache_dir: Path) -> None:
    cache = ParquetCache(cache_dir)
    for code in _SYMBOLS:
        symbol = Symbol(code, Market.A_SHARE)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=Decimal("1000"),
                amount=Decimal("10500"),
            )
            for offset in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)


async def test_service_publishes_files_and_queryable_immutable_record(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
    test_stock: None,
) -> None:
    del test_stock
    await _seed_bars(tmp_path / "cache")

    service = ResearchDatasetReleaseService(
        db_session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    release = await service.publish(
        DatasetReleaseSpec(
            release_id="integration-r77-v1",
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version="integration-v1",
            start_date=_START,
            end_date=_END,
            code_version="integration-test",
        ),
        _SYMBOLS,
    )
    await db_session.commit()

    repo = ResearchDatasetReleaseRepository(db_session)
    restored = await repo.require_usable(
        release.release_id,
        capabilities=(
            "stock",
            "etf:index",
            "etf:cross_border",
            "etf:commodity",
            "etf:bond",
        ),
    )
    assert restored == release
    assert restored.instrument("TST077.SH").available_at == datetime(
        2020, 1, 1, tzinfo=UTC
    )
    assert (tmp_path / "releases" / release.release_id / "manifest.json").is_file()
    listed = await repo.list(dataset_name="multi_asset_daily_bars")
    assert [item.release_id for item in listed] == [release.release_id]

    with pytest.raises(ImmutableReleaseError, match="checksum 不同"):
        await repo.publish(replace(release, release_checksum="0" * 64))

    historical = await service.publish(
        DatasetReleaseSpec(
            release_id="integration-r77-v2",
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version="integration-v2",
            start_date=_START,
            end_date=_END,
            code_version="integration-test-v2",
        ),
        _SYMBOLS,
    )
    await db_session.commit()

    # 模拟进程重启:新建 session 后按 ID 读取同一 checksum,历史版本同时可用。
    async with session_factory(_engine)() as restarted_session:
        restarted_repo = ResearchDatasetReleaseRepository(restarted_session)
        assert await restarted_repo.get(release.release_id) == release
        assert await restarted_repo.get(historical.release_id) == historical
        restarted_list = await restarted_repo.list(
            dataset_name="multi_asset_daily_bars"
        )
        assert {item.release_id for item in restarted_list} == {
            release.release_id,
            historical.release_id,
        }

    row_count = (
        await db_session.execute(
            select(func.count()).select_from(ResearchDatasetReleaseModel)
        )
    ).scalar_one()
    assert row_count == 2

    await db_session.execute(
        delete(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.release_id.in_(
                (release.release_id, historical.release_id)
            )
        )
    )
    await db_session.flush()
