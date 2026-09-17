"""``research_factor_series`` repository 集成测试(需 PostgreSQL,issue #360)。

覆盖:upsert 幂等(内容寻址)/ get / find_matching(series_key 精确命中)/
list_for_release / list_for_artifact / series_coverage_missing 落库回读 /
content_checksum 冲突 fail-closed。并发 upsert 用双 engine 会话模拟两个
worker 互相看不到对方未提交行的竞态(#204 ON CONFLICT RETURNING 复查先例)。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence import (
    FactorSeriesConflictError,
    FactorSeriesRecord,
    FactorSeriesRepository,
    compute_content_checksum,
    series_coverage_missing,
)
from finboard_persistence.session import session_factory
from tests.integration.conftest import TEST_DB_URL

_COMMIT = "c" * 40


def _record(
    *,
    params: dict[str, object] | None = None,
    release_id: str = "DR-bars-1",
    commit: str = _COMMIT,
    dates: list[date] | None = None,
    name: str = "mom20",
) -> FactorSeriesRecord:
    effective_dates = dates or [date(2024, 1, 31), date(2024, 2, 29)]
    values = {
        day.isoformat(): {"600000.SH": float(index), "000001.SZ": None}
        for index, day in enumerate(effective_dates)
    }
    return FactorSeriesRecord.build(
        code_artifact=name,
        code_commit=commit,
        kind="factor",
        release_id=release_id,
        dataset_release_ids=("DR-daily-1", "DR-fin-1"),
        params=params or {"window": 20},
        window_start=date(2022, 1, 1),
        window_end=date(2023, 6, 30),
        dates=effective_dates,
        values=values,
        quality={"nan_ratio": 0.0, "coverage": 1.0},
        source_run_id="RCR-360test",
    )


class TestUpsertAndGet:
    async def test_upsert_idempotent_round_trip(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        first = await repo.upsert(_record())
        await db_session.commit()
        assert first.series_id.startswith("FS-")
        assert len(first.series_id) == 15  # FS- + 12

        # 同 key 同内容:复用原记录(幂等),不新增行。
        second = await repo.upsert(_record())
        await db_session.commit()
        assert second.series_id == first.series_id
        assert second.series_key == first.series_key

        # 回读逐列一致(dates/values 反序列化)。
        loaded = await repo.get(first.series_id)
        assert loaded is not None
        assert loaded == first
        assert loaded.dates == (date(2024, 1, 31), date(2024, 2, 29))
        assert loaded.values["2024-01-31"] == {"600000.SH": 0.0, "000001.SZ": None}
        assert loaded.quality == {"nan_ratio": 0.0, "coverage": 1.0}
        assert loaded.source_run_id == "RCR-360test"
        assert loaded.created_at is not None
        assert loaded.updated_at is not None

    async def test_get_missing_none(self, db_session: AsyncSession):
        assert await FactorSeriesRepository(db_session).get("FS-missing12345") is None

    async def test_checksum_conflict_fail_closed(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        await repo.upsert(_record(params={"window": 20}))
        await db_session.commit()
        # 同 series_key(series_key 不含 dates)但不同 values/dates(确定性
        # 破坏)→ 复查发现 content_checksum 不一致。
        tampered_values = {"2024-01-31": {"600000.SH": 999.0, "000001.SZ": None}}
        base_record = _record(params={"window": 20})
        tampered = FactorSeriesRecord(
            series_id=base_record.series_id,
            series_key=base_record.series_key,
            code_artifact=base_record.code_artifact,
            code_commit=base_record.code_commit,
            kind=base_record.kind,
            release_id=base_record.release_id,
            dataset_release_ids=base_record.dataset_release_ids,
            params=base_record.params,
            window_start=base_record.window_start,
            window_end=base_record.window_end,
            dates=(date(2024, 1, 31),),
            values=tampered_values,
            content_checksum=compute_content_checksum(
                [date(2024, 1, 31)], tampered_values
            ),
        )
        with pytest.raises(FactorSeriesConflictError, match="content_checksum 不一致"):
            await repo.upsert(tampered)


class TestFindMatching:
    async def test_exact_key_match_and_window_no_fuzzy(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        record = await repo.upsert(_record(params={"window": 20}))
        await db_session.commit()

        hit = await repo.find_matching(
            code_artifact="mom20",
            release_id="DR-bars-1",
            dataset_release_ids=("DR-daily-1", "DR-fin-1"),
            params={"window": 20},
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        assert hit is not None
        assert hit.series_id == record.series_id

        # 内容寻址精确命中:窗口/params/发布任一不同 → 不模糊匹配。
        assert (
            await repo.find_matching(
                code_artifact="mom20",
                release_id="DR-bars-1",
                dataset_release_ids=("DR-daily-1", "DR-fin-1"),
                params={"window": 20},
                window_start=date(2022, 1, 1),
                window_end=date(2023, 7, 1),
            )
            is None
        )
        assert (
            await repo.find_matching(
                code_artifact="mom20",
                release_id="DR-bars-1",
                dataset_release_ids=("DR-daily-1", "DR-fin-1"),
                params={"window": 5},
                window_start=date(2022, 1, 1),
                window_end=date(2023, 6, 30),
            )
            is None
        )
        assert (
            await repo.find_matching(
                code_artifact="mom20",
                release_id="DR-bars-2",
                dataset_release_ids=("DR-daily-1", "DR-fin-1"),
                params={"window": 20},
                window_start=date(2022, 1, 1),
                window_end=date(2023, 6, 30),
            )
            is None
        )

    async def test_match_survives_joint_order_drift(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        record = await repo.upsert(_record())
        await db_session.commit()
        hit = await repo.find_matching(
            code_artifact="mom20",
            release_id="DR-bars-1",
            dataset_release_ids=("DR-fin-1", "DR-daily-1"),  # 顺序漂移
            params={"window": 20},
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        assert hit is not None
        assert hit.series_id == record.series_id


class TestListQueries:
    async def test_list_for_release_and_artifact(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        await repo.upsert(_record(release_id="DR-bars-1"))
        await repo.upsert(_record(release_id="DR-bars-2", name="vol20"))
        await db_session.commit()
        assert len(await repo.list_for_release("DR-bars-1")) == 1
        assert len(await repo.list_for_release("DR-bars-2")) == 1
        assert len(await repo.list_for_release("DR-bars-3")) == 0
        assert len(await repo.list_for_artifact("mom20")) == 1
        assert len(await repo.list_for_artifact("vol20")) == 1


class TestCoverageOnRecord:
    async def test_coverage_missing_after_round_trip(self, db_session: AsyncSession):
        repo = FactorSeriesRepository(db_session)
        record = await repo.upsert(
            _record(dates=[date(2024, 1, 31), date(2024, 2, 29)])
        )
        await db_session.commit()
        loaded = await repo.get(record.series_id)
        assert loaded is not None
        assert series_coverage_missing(
            loaded,
            [date(2024, 1, 31), date(2024, 3, 29), date(2024, 4, 1)],
        ) == [date(2024, 3, 29), date(2024, 4, 1)]


class TestConcurrentUpsert:
    async def test_two_sessions_same_key_converge(self) -> None:
        """双会话并发 upsert 同一 series_key(#204 ON CONFLICT 复查先例)。

        会话 A 先插不提交;会话 B(RREAD COMMITTED,独立 engine 连接)插入
        同 key 阻塞至 A 提交后走 ON CONFLICT 复查路径,双方拿到同一
        series_id;checksum 一致不抛冲突。
        """
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine as sa_create

        target_url = TEST_DB_URL
        engine_a = sa_create(target_url)
        engine_b = sa_create(target_url)
        smaker_a = session_factory(engine_a)
        smaker_b = session_factory(engine_b)
        record = _record(params={"concurrent": True})
        try:
            async def _insert_a() -> str:
                async with smaker_a() as session:
                    row = await FactorSeriesRepository(session).upsert(record)
                    await asyncio.sleep(0.05)  # 拉开 A 提交与 B 插入的窗口
                    await session.commit()
                    return row.series_id

            async def _insert_b() -> str:
                await asyncio.sleep(0.02)  # B 在 A 未提交时进入
                async with smaker_b() as session:
                    row = await FactorSeriesRepository(session).upsert(record)
                    await session.commit()
                    return row.series_id

            id_a, id_b = await asyncio.gather(_insert_a(), _insert_b())
            assert id_a == id_b
            assert id_a == record.series_id
        finally:
            # 清理并发插入的行(该 params 不与其它用例共用)。
            from sqlalchemy import text

            async with engine_a.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM research_factor_series "
                        "WHERE code_artifact = 'mom20' AND params->>'concurrent' = 'true'"
                    )
                )
            await engine_a.dispose()
            await engine_b.dispose()


def _record_fields(record: FactorSeriesRecord) -> dict[str, object]:
    from dataclasses import asdict

    return asdict(record)
