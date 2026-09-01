"""instruments 主数据治理集成测试(issue #251)。

覆盖:
* ``backfill_metadata_from_profiles`` 不再依赖「已发布 profiles 批次」——
  RUNNING 批次(摄取过但从未发布)的数据即回填(#251 断点修复);
* ``delist_date`` 自退市档案(list_status=D)回填,``status`` 不被改动
  (缺席二次确认是退市状态的唯一权威路径);
* ``import_name_history`` 以 tushare namechange 半开区间重建
  ``instrument_names``,未提及 symbol 的既有记录保留,重跑幂等。
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
    InstrumentNameModel,
    InstrumentRepository,
    ResearchDataset,
    ResearchDatasetRepository,
    ResearchSyncBatchRepository,
)
from finboard_shared.types import ListingStatus

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _ingest_profiles(
    session: AsyncSession,
    *,
    dataset_version: str,
    records: list[InstrumentProfile],
    publish: bool,
) -> None:
    """在当前事务内走 prepare → 写入(→ 可选 mark_published)流。"""
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
    if publish:
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
    list_status: str = "L",
    delist_date: date | None = None,
) -> InstrumentProfile:
    return InstrumentProfile(
        symbol=symbol,
        name=name,
        exchange="SZSE",
        market="主板",
        list_status=list_status,
        list_date=list_date,
        delist_date=delist_date,
        industry=industry,
        source="tushare",
        observed_at=_NOW,
        available_at=_NOW,
    )


async def _sync_instruments(
    session: AsyncSession, codes: list[str], *, as_of: date
) -> None:
    await InstrumentRepository(session).sync_with_diff(
        [
            {"code": code, "name": f"name-{code}", "market": "a_share",
             "instrument_type": "stock"}
            for code in codes
        ],
        as_of=as_of,
    )


class TestBackfillFromUnpublishedBatch:
    async def test_running_batch_backfills_list_date_and_industry(
        self, db_session: AsyncSession
    ) -> None:
        """#251 断点修复:批次 RUNNING(未发布)时回填照常生效。"""
        await _ingest_profiles(
            db_session,
            dataset_version="issue251-unpublished-v1",
            records=[
                _profile(
                    "000001.SZ",
                    name="平安银行",
                    list_date=date(1991, 4, 3),
                    industry="银行",
                )
            ],
            publish=False,
        )
        await _sync_instruments(db_session, ["000001.SZ"], as_of=date(2026, 9, 1))

        repo = InstrumentRepository(db_session)
        result = await repo.backfill_metadata_from_profiles()

        assert result.profile_batch_available is True
        assert result.backfilled_list_date == 1
        assert result.backfilled_industry == 1
        row = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "000001.SZ")
            )
        ).scalar_one()
        assert row.list_date == date(1991, 4, 3)
        assert row.industry == "银行"


class TestDelistDateBackfill:
    async def test_delisted_profile_backfills_delist_date_not_status(
        self, db_session: AsyncSession
    ) -> None:
        """#251:退市档案回填 delist_date;status 仍由缺席二次确认决定。"""
        await _ingest_profiles(
            db_session,
            dataset_version="issue251-delist-v1",
            records=[
                _profile(
                    "000003.SZ",
                    name="PT金田A",
                    list_date=date(1991, 7, 3),
                    industry=None,
                    list_status="D",
                    delist_date=date(2002, 6, 14),
                ),
            ],
            publish=True,
        )
        await _sync_instruments(db_session, ["000003.SZ"], as_of=date(2026, 9, 1))

        repo = InstrumentRepository(db_session)
        result = await repo.backfill_metadata_from_profiles()

        assert result.backfilled_delist_date == 1
        row = (
            await db_session.execute(
                select(InstrumentModel).where(InstrumentModel.code == "000003.SZ")
            )
        ).scalar_one()
        assert row.delist_date == date(2002, 6, 14)
        # status 不因档案回填变更:退市状态由 sync_with_diff 缺席二次确认推进。
        assert row.status == ListingStatus.ACTIVE.value


class TestImportNameHistory:
    async def test_rebuilds_intervals_and_keeps_unmentioned_symbols(
        self, db_session: AsyncSession
    ) -> None:
        """#251:半开区间重建;未提及 symbol 保留既有记录;重跑幂等。"""
        await _sync_instruments(
            db_session, ["000001.SZ", "600000.SH"], as_of=date(2026, 9, 1)
        )

        repo = InstrumentRepository(db_session)
        records = [
            ("000001.SZ", "深发展A", date(1991, 4, 3), date(1992, 3, 9)),
            # 同起始日重复行:保留最后一条(上游修订)。
            ("000001.SZ", "深发展A(修订)", date(1991, 4, 3), date(1992, 3, 9)),
            ("000001.SZ", "平安银行", date(1992, 3, 9), None),
        ]
        first = await repo.import_name_history(records)
        assert first.rebuilt_symbols == 1
        # 去重后 000001.SZ 两条;600000.SH 未提及。
        assert first.inserted_records == 2

        names_000001 = (
            (
                await db_session.execute(
                    select(InstrumentNameModel)
                    .where(InstrumentNameModel.instrument_code == "000001.SZ")
                    .order_by(InstrumentNameModel.valid_from)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.name, row.valid_from, row.valid_to) for row in names_000001] == [
            ("深发展A(修订)", date(1991, 4, 3), date(1992, 3, 9)),
            ("平安银行", date(1992, 3, 9), None),
        ]

        # 未提及 symbol 的既有记录(sync_with_diff 写入的当前名称)保留。
        names_600000 = (
            (
                await db_session.execute(
                    select(InstrumentNameModel).where(
                        InstrumentNameModel.instrument_code == "600000.SH"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(names_600000) == 1

        # 重跑幂等:不产生重复行。
        await repo.import_name_history(records)
        count = len(
            (
                await db_session.execute(
                    select(InstrumentNameModel).where(
                        InstrumentNameModel.instrument_code == "000001.SZ"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert count == 2

    async def test_missing_intermediate_valid_to_filled_from_next_start(
        self, db_session: AsyncSession
    ) -> None:
        """#251:非末行缺 valid_to 时用下一行起始日补齐(区间连续)。"""
        await _sync_instruments(db_session, ["000001.SZ"], as_of=date(2026, 9, 1))

        repo = InstrumentRepository(db_session)
        await repo.import_name_history(
            [
                ("000001.SZ", "旧名", date(1991, 4, 3), None),
                ("000001.SZ", "现名", date(2012, 5, 7), None),
            ]
        )

        names = (
            (
                await db_session.execute(
                    select(InstrumentNameModel)
                    .where(InstrumentNameModel.instrument_code == "000001.SZ")
                    .order_by(InstrumentNameModel.valid_from)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.name, row.valid_to) for row in names] == [
            ("旧名", date(2012, 5, 7)),  # 补齐自下一行起始日
            ("现名", None),
        ]
