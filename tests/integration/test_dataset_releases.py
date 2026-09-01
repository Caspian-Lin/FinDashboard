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
from finboard_data.research import InstrumentProfile
from finboard_persistence import (
    InstrumentModel,
    ResearchDataset,
    ResearchDatasetReleaseModel,
    ResearchDatasetReleaseRepository,
    ResearchDatasetReleaseService,
    ResearchDatasetRepository,
    ResearchSyncBatchRepository,
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
                source="fixed_sample",
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
    listed = await repo.list(
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
    )
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
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
        )
        assert {item.release_id for item in restarted_list} == {
            release.release_id,
            historical.release_id,
        }

    row_count = (
        await db_session.execute(
            select(func.count())
            .select_from(ResearchDatasetReleaseModel)
            .where(ResearchDatasetReleaseModel.source == "fixed_sample")
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
    await db_session.commit()


async def _publish_profiles(
    session: AsyncSession,
    *,
    dataset_version: str,
    records: list[InstrumentProfile],
) -> None:
    """在当前事务内发布一批 instrument profiles(issue #185 兜底源)。"""
    batches = ResearchSyncBatchRepository(session)
    batch, already = await batches.prepare(
        dataset=ResearchDataset.INSTRUMENT_PROFILES,
        source="tushare",
        dataset_version=dataset_version,
        code_version="test",
        parameters={},
        raw_payload=None,
        expected_rows=len(records),
        received_rows=len(records),
    )
    assert not already
    locked = await batches.get(batch.id, for_update=True)
    assert locked is not None
    accepted = await ResearchDatasetRepository(session).upsert_instrument_profiles(
        locked, records
    )
    await batches.mark_published(
        batch.id,
        accepted_rows=accepted,
        quality_status="passed",
        quality_report={},
    )


async def test_release_instruments_fallback_to_profiles_for_metadata(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """instruments 的 list_date/industry 为 null 时,发布产物从 profiles 兜底(issue #185)。"""
    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "TST077.SH")
    )
    db_session.add(
        InstrumentModel(
            code="TST077.SH",
            name="issue185 股票样本",
            market="a_share",
            instrument_type="stock",
            exchange="SSE",
            list_date=None,  # akshare 发现链路不携带该字段
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    await _publish_profiles(
        db_session,
        dataset_version="issue185-release-v1",
        records=[
            InstrumentProfile(
                symbol="TST077.SH",
                name="issue185 股票样本",
                exchange="SSE",
                market="主板",
                list_status="L",
                list_date=date(2020, 1, 1),
                delist_date=None,
                industry="银行",
                source="tushare",
                observed_at=datetime(2024, 1, 1, tzinfo=UTC),
                available_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
    )
    await db_session.flush()

    await _seed_bars(tmp_path / "cache")
    service = ResearchDatasetReleaseService(
        db_session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    release = await service.publish(
        DatasetReleaseSpec(
            release_id="integration-r185-fallback",
            dataset_name="multi_asset_daily_bars",
            source="fixed_sample",
            version="integration-r185",
            start_date=_START,
            end_date=_END,
            code_version="integration-test",
            required_capabilities=("stock",),
        ),
        ["TST077.SH"],
    )
    try:
        instrument = release.instrument("TST077.SH")
        assert instrument.list_date == date(2020, 1, 1)  # 从 profiles 兜底
        assert instrument.industry == "银行"
        assert release.quality_report["instrument_metadata"] == {
            "total": 1,
            "missing_list_date": 0,
            "missing_industry": 0,
            # #251:delist_date 缺失与名称历史覆盖同样进入质量报告。
            # 该样本档案无 delist_date、instruments 无名称历史 → 缺失可见。
            "missing_delist_date": 1,
            "with_name_history": 0,
            "name_history_coverage": "0.0000",
        }
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(ResearchDatasetReleaseModel).where(
                ResearchDatasetReleaseModel.release_id
                == "integration-r185-fallback"
            )
        )
        await db_session.execute(
            delete(InstrumentModel).where(InstrumentModel.code == "TST077.SH")
        )
        await db_session.commit()


async def test_index_benchmark_symbol_publishable(
    _engine: AsyncEngine,  # noqa: PT019 - 共享集成测试 fixture 的既有命名
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """指数基准资产(000300.SH)可进发布通道(issue #184)。

    指数不来自 ETF 目录,必须显式登记 instruments 元数据才能发布;
    发布后带 ``index`` 能力(READY)、资产类 EQUITY、零费用执行占位,
    冻结 bars 可经 FrozenReleaseProvider 读取。
    """
    from finboard_data import FrozenReleaseProvider
    from finboard_data.releases import CapabilityStatus
    from finboard_persistence import InstrumentModel

    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "000300.SH")
    )
    db_session.add(
        InstrumentModel(
            code="000300.SH",
            name="沪深300指数",
            market="a_share",
            instrument_type="index",
            exchange="SSE",
            list_date=date(2005, 4, 8),
            status="active",
            updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.flush()

    cache = ParquetCache(tmp_path / "cache")
    symbol = Symbol("000300.SH", Market.A_SHARE)
    bars = []
    for offset in range(4):
        close = Decimal("3800") + Decimal(offset) * Decimal("100")
        bars.append(
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=close - Decimal("5"),
                high=close + Decimal("12"),
                low=close - Decimal("15"),
                close=close,
                volume=Decimal("100000000"),
                amount=Decimal("0"),
                source="fixed_sample",
            )
        )
    await cache.write(symbol, BarPeriod.D1, "qfq", bars)

    service = ResearchDatasetReleaseService(
        db_session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    release = await service.publish(
        DatasetReleaseSpec(
            release_id="integration-r184-index",
            dataset_name="benchmark_index_daily_bars",
            source="fixed_sample",
            version="integration-index-v1",
            start_date=_START,
            end_date=_END,
            code_version="integration-test",
            required_capabilities=("index",),
        ),
        ["000300.SH"],
    )
    await db_session.commit()

    item = release.instrument("000300.SH")
    assert item is not None
    assert item.asset_class == "equity"
    assert item.execution.commission_rate == Decimal("0")
    capability = next(c for c in release.capabilities if c.key == "index")
    assert capability.status is CapabilityStatus.READY

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=release.release_id,
    )
    bars = await provider.fetch_bars(symbol, BarPeriod.D1, _START, _END)
    assert len(bars) == 4

    await db_session.execute(
        delete(ResearchDatasetReleaseModel).where(
            ResearchDatasetReleaseModel.release_id == "integration-r184-index"
        )
    )
    await db_session.execute(
        delete(InstrumentModel).where(InstrumentModel.code == "000300.SH")
    )
    await db_session.commit()
