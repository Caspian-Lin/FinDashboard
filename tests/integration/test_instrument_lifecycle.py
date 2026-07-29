"""标的生命周期检测集成测试(issue #35)。

覆盖 :meth:`InstrumentRepository.sync_with_diff`(退市 diff / 改名追踪)、
``instrument_names`` 历史表、停牌状态更新。需 PostgreSQL。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from finboard_persistence import InstrumentModel, InstrumentNameModel, InstrumentRepository
from finboard_shared.types import ListingStatus

pytestmark = pytest.mark.asyncio


@pytest.fixture
def as_of() -> date:
    return date(2026, 7, 29)


def _ins(code: str, name: str, market: str = "a_share", itype: str = "stock") -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "market": market,
        "instrument_type": itype,
        "exchange": "SSE" if code.endswith(".SH") else "SZSE",
    }


async def _purge(session) -> None:
    await session.execute(delete(InstrumentNameModel))
    await session.execute(delete(InstrumentModel))
    await session.commit()


class TestSyncWithDiffInsert:
    async def test_new_instruments_inserted(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "平安银行"), _ins("600000.SH", "浦发银行")],
            as_of=as_of,
        )
        await db_session.commit()

        assert result.new == 2
        assert result.updated == 0
        assert result.delisted == []
        row = await repo.get_by_code("000001.SZ")
        assert row is not None
        assert row.status == ListingStatus.ACTIVE.value
        assert row.missing_runs == 0

    async def test_empty_input_returns_empty(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        result = await repo.sync_with_diff([], as_of=as_of)
        assert result.total == 0


class TestSyncWithDiffRename:
    async def test_rename_creates_name_history(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        # 第一次:插入旧名
        await repo.sync_with_diff([_ins("000001.SZ", "旧名称")], as_of=date(2026, 7, 1))
        await db_session.commit()

        # 第二次:改名
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "新名称")], as_of=as_of
        )
        await db_session.commit()

        assert len(result.renamed) == 1
        code, old, new = result.renamed[0]
        assert code == "000001.SZ"
        assert old == "旧名称"
        assert new == "新名称"

        row = await repo.get_by_code("000001.SZ")
        assert row is not None
        assert row.name == "新名称"  # instruments.name 保持最新

        history = await repo.name_history("000001.SZ")
        assert len(history) == 2
        # 旧记录 valid_to 已关闭
        assert history[0].name == "旧名称"
        assert history[0].valid_to == as_of
        # 新记录 valid_to 仍为 None
        assert history[1].name == "新名称"
        assert history[1].valid_to is None

    async def test_same_name_no_rename(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff([_ins("000001.SZ", "不变")], as_of=date(2026, 7, 1))
        await db_session.commit()
        result = await repo.sync_with_diff([_ins("000001.SZ", "不变")], as_of=as_of)
        await db_session.commit()
        assert result.renamed == []
        history = await repo.name_history("000001.SZ")
        assert len(history) == 1


class TestSyncWithDiffDelist:
    async def test_missing_first_run_pending(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        # 初始:两只股票
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 第一次消失:000002 不在列表 → pending(未达阈值)
        result = await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=as_of)
        await db_session.commit()

        assert result.pending_delist == ["000002.SZ"]
        assert result.delisted == []
        row = await repo.get_by_code("000002.SZ")
        assert row is not None
        assert row.status == ListingStatus.ACTIVE.value
        assert row.missing_runs == 1
        assert row.delist_date is None

    async def test_missing_second_run_delisted(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 连续 2 次消失(默认阈值 2)→ delisted
        await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=date(2026, 7, 28))
        await db_session.commit()
        result = await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=as_of)
        await db_session.commit()

        assert result.delisted == ["000002.SZ"]
        row = await repo.get_by_code("000002.SZ")
        assert row is not None
        assert row.status == ListingStatus.DELISTED.value
        assert row.delist_date == as_of

    async def test_reactived_before_threshold(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 消失一次(pending)
        await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=date(2026, 7, 28))
        await db_session.commit()
        # 重新出现 → 计数归零,不退市
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")], as_of=as_of
        )
        await db_session.commit()

        assert result.reactivated == ["000002.SZ"]
        row = await repo.get_by_code("000002.SZ")
        assert row is not None
        assert row.status == ListingStatus.ACTIVE.value
        assert row.missing_runs == 0

    async def test_delisted_not_reactivated_automatically(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 连续 2 次消失 → delisted
        await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=date(2026, 7, 28))
        await db_session.commit()
        await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=date(2026, 7, 29))
        await db_session.commit()
        # 重新出现:归零计数但不自动复活(退市严肃事件需人工确认)
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")], as_of=as_of
        )
        await db_session.commit()

        assert result.reactivated == []
        row = await repo.get_by_code("000002.SZ")
        assert row is not None
        assert row.status == ListingStatus.DELISTED.value
        assert row.missing_runs == 0

    async def test_custom_confirm_runs(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 阈值=1:消失一次即退市
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "A")], as_of=as_of, delist_confirm_runs=1
        )
        await db_session.commit()
        assert result.delisted == ["000002.SZ"]

    async def test_invalid_confirm_runs(self, db_session, as_of) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        with pytest.raises(ValueError, match="delist_confirm_runs"):
            await repo.sync_with_diff(
                [_ins("000001.SZ", "A")], as_of=as_of, delist_confirm_runs=0
            )


class TestSyncWithDiffScope:
    async def test_only_a_share_scope_not_affect_etf(self, db_session, as_of) -> None:
        """只拉 A 股股票列表时,不应把 ETF 标记为退市。"""
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        # 预置一只股票 + 一只 ETF
        await repo.sync_with_diff(
            [
                _ins("000001.SZ", "股票A", itype="stock"),
                _ins("510300.SH", "沪深300ETF", itype="etf"),
            ],
            as_of=date(2026, 7, 1),
        )
        await db_session.commit()
        # 本次只发现股票(模拟 discover_a_shares),ETF 不在列表
        result = await repo.sync_with_diff(
            [_ins("000001.SZ", "股票A", itype="stock")], as_of=as_of
        )
        await db_session.commit()

        # ETF 不在本次 scope(stock),不应被标记 pending_delist
        assert result.pending_delist == []
        etf = await repo.get_by_code("510300.SH")
        assert etf is not None
        assert etf.status == ListingStatus.ACTIVE.value
        assert etf.missing_runs == 0


class TestUpdateListingStatus:
    async def test_suspend_and_resume(self, db_session) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff([_ins("000001.SZ", "A")], as_of=date(2026, 7, 1))
        await db_session.commit()

        assert await repo.update_listing_status("000001.SZ", ListingStatus.SUSPENDED)
        await db_session.commit()
        assert (await repo.get_by_code("000001.SZ")).status == ListingStatus.SUSPENDED.value  # type: ignore[union-attr]

        assert await repo.update_listing_status(
            "000001.SZ", ListingStatus.ACTIVE, reset_missing_runs=True
        )
        await db_session.commit()
        row = await repo.get_by_code("000001.SZ")
        assert row.status == ListingStatus.ACTIVE.value  # type: ignore[union-attr]
        assert row.missing_runs == 0  # type: ignore[union-attr]

    async def test_nonexistent_returns_false(self, db_session) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        assert await repo.update_listing_status("999999.SZ", ListingStatus.SUSPENDED) is False


class TestGetStatusMap:
    async def test_status_map(self, db_session) -> None:
        await _purge(db_session)
        repo = InstrumentRepository(db_session)
        await repo.sync_with_diff(
            [_ins("000001.SZ", "A"), _ins("000002.SZ", "B")], as_of=date(2026, 7, 1)
        )
        await db_session.commit()
        await repo.update_listing_status("000001.SZ", ListingStatus.SUSPENDED)
        await db_session.commit()

        status_map = await repo.get_status_map({"000001.SZ", "000002.SZ", "999999.SZ"})
        assert status_map["000001.SZ"] == ListingStatus.SUSPENDED.value
        assert status_map["000002.SZ"] == ListingStatus.ACTIVE.value
        assert "999999.SZ" not in status_map
