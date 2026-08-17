"""instrument 元数据回填集成测试(issue #185)。

覆盖 :meth:`InstrumentRepository.backfill_metadata_from_profiles`:
* 已发布 profiles 批次 → instruments.list_date/industry 回填 + 缺失统计;
* 未发布过 profiles(无兜底源)→ 不报错,``profile_batch_available=False``;
* 已存在的主数据不被覆盖;
* 档案缺 industry → 只回填 list_date,缺失统计保留。
需 PostgreSQL。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.research import InstrumentProfile
from finboard_persistence import (
    InstrumentModel,
    InstrumentRepository,
    ResearchDataset,
    ResearchDatasetRepository,
    ResearchSyncBatchRepository,
)
from finboard_shared.types import ListingStatus

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


async def _publish_profiles(
    session: AsyncSession,
    *,
    dataset_version: str,
    records: list[InstrumentProfile],
) -> None:
    """在当前事务内走 prepare → 写入 → mark_published 的完整发布流。"""
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


def _profile(
    symbol: str,
    *,
    name: str,
    list_date: date,
    industry: str | None,
) -> InstrumentProfile:
    return InstrumentProfile(
        symbol=symbol,
        name=name,
        exchange="SZSE",
        market="主板",
        list_status="L",
        list_date=list_date,
        delist_date=None,
        industry=industry,
        source="tushare",
        observed_at=_NOW,
        available_at=_NOW,
    )


class TestBackfillMetadataFromProfiles:
    async def test_backfills_list_date_and_industry(
        self, db_session: AsyncSession
    ) -> None:
        await _publish_profiles(
            db_session,
            dataset_version="issue185-v1",
            records=[
                _profile(
                    "000001.SZ",
                    name="平安银行",
                    list_date=date(1991, 4, 3),
                    industry="银行",
                ),
                _profile(
                    "600000.SH",
                    name="浦发银行",
                    list_date=date(1999, 11, 10),
                    industry="银行",
                ),
            ],
        )
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [
                {"code": "000001.SZ", "name": "平安银行", "market": "a_share",
                 "instrument_type": "stock"},
                {"code": "600000.SH", "name": "浦发银行", "market": "a_share",
                 "instrument_type": "stock"},
            ],
            as_of=date(2026, 8, 4),
        )

        result = await repo.backfill_metadata_from_profiles()

        assert result.profile_batch_available is True
        assert result.scoped == 2
        assert result.backfilled_list_date == 2
        assert result.backfilled_industry == 2
        assert result.missing_list_date == 0
        assert result.missing_industry == 0
        rows = (
            (await db_session.execute(select(InstrumentModel))).scalars().all()
        )
        by_code = {row.code: row for row in rows}
        assert by_code["000001.SZ"].list_date == date(1991, 4, 3)
        assert by_code["000001.SZ"].industry == "银行"
        assert by_code["600000.SH"].list_date == date(1999, 11, 10)
        assert by_code["600000.SH"].industry == "银行"

    async def test_does_not_overwrite_existing_metadata(
        self, db_session: AsyncSession
    ) -> None:
        await _publish_profiles(
            db_session,
            dataset_version="issue185-v2",
            records=[
                _profile(
                    "000001.SZ",
                    name="平安银行",
                    list_date=date(1991, 4, 3),
                    industry="银行",
                )
            ],
        )
        db_session.add(
            InstrumentModel(
                code="000001.SZ",
                name="平安银行",
                market="a_share",
                instrument_type="stock",
                list_date=date(1990, 1, 1),  # 已有权威主数据
                status=ListingStatus.ACTIVE.value,
            )
        )
        # industry 为 null,应回填;list_date 已有值,不应被覆盖。
        db_session.add(
            InstrumentModel(
                code="600000.SH",
                name="浦发银行",
                market="a_share",
                instrument_type="stock",
                list_date=date(1999, 11, 10),
                industry="银行",
                status=ListingStatus.ACTIVE.value,
            )
        )
        await db_session.flush()

        repo = InstrumentRepository(db_session)
        result = await repo.backfill_metadata_from_profiles()

        row = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "000001.SZ")
            )
        ).scalar_one()
        assert row.list_date == date(1990, 1, 1)  # 已有主数据不被覆盖
        assert row.industry == "银行"  # 原本为 null,回填
        kept = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "600000.SH")
            )
        ).scalar_one()
        assert kept.list_date == date(1999, 11, 10)
        assert result.backfilled_list_date == 0
        assert result.backfilled_industry == 1

    async def test_no_published_profiles_reports_unavailable(
        self, db_session: AsyncSession
    ) -> None:
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [
                {
                    "code": "000001.SZ",
                    "name": "平安银行",
                    "market": "a_share",
                    "instrument_type": "stock",
                }
            ],
            as_of=date(2026, 8, 4),
        )

        result = await repo.backfill_metadata_from_profiles()

        assert result.profile_batch_available is False
        assert result.scoped == 0
        row = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "000001.SZ")
            )
        ).scalar_one()
        assert row.list_date is None  # 无兜底源时保持原样,不报错

    async def test_missing_industry_kept_visible(
        self, db_session: AsyncSession
    ) -> None:
        await _publish_profiles(
            db_session,
            dataset_version="issue185-v3",
            records=[
                _profile(
                    "000001.SZ",
                    name="平安银行",
                    list_date=date(1991, 4, 3),
                    industry=None,
                )
            ],
        )
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [
                {
                    "code": "000001.SZ",
                    "name": "平安银行",
                    "market": "a_share",
                    "instrument_type": "stock",
                },
                {
                    "code": "600000.SH",
                    "name": "浦发银行",
                    "market": "a_share",
                    "instrument_type": "stock",
                },
            ],
            as_of=date(2026, 8, 4),
        )

        result = await repo.backfill_metadata_from_profiles()

        assert result.backfilled_list_date == 1
        assert result.backfilled_industry == 0
        assert result.missing_list_date == 1  # 600000.SH 无档案
        assert result.missing_industry == 2  # 档案缺 industry + 无档案

    async def test_symbols_scoping(self, db_session: AsyncSession) -> None:
        await _publish_profiles(
            db_session,
            dataset_version="issue185-v4",
            records=[
                _profile(
                    "000001.SZ",
                    name="平安银行",
                    list_date=date(1991, 4, 3),
                    industry="银行",
                ),
                _profile(
                    "600000.SH",
                    name="浦发银行",
                    list_date=date(1999, 11, 10),
                    industry="银行",
                ),
            ],
        )
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [
                {"code": "000001.SZ", "name": "平安银行", "market": "a_share",
                 "instrument_type": "stock"},
                {"code": "600000.SH", "name": "浦发银行", "market": "a_share",
                 "instrument_type": "stock"},
            ],
            as_of=date(2026, 8, 4),
        )

        result = await repo.backfill_metadata_from_profiles(symbols=["600000.SH"])

        assert result.scoped == 1
        assert result.backfilled_list_date == 1
        row = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "000001.SZ")
            )
        ).scalar_one()
        assert row.list_date is None  # 不在作用域内,不触碰
