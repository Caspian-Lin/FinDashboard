"""issue #395 执行器层单元测试:data_sync 期货合约回填 + fut_trade_cal 落库。

与 :file:`test_issue_394_data_executors.py` 同风格(打桩 discovery /
InstrumentRepository / TradeCalRepository,离线驱动 DataSyncExecutor)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finboard_backtest.background_jobs.contracts import JobResult
from finboard_backtest.background_jobs.executors.data_sync import (
    DataSyncExecutor,
    _fetch_futures_trade_calendar,
)
from finboard_data.discovery import InstrumentInfo
from finboard_data.research import FuturesTradeCalendarDay


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 FINBOARD_DATA_PROVIDER(.env 可能携带)。"""
    monkeypatch.delenv("FINBOARD_DATA_PROVIDER", raising=False)


class _FakeRepo:
    def __init__(self, session: object) -> None:
        self.synced: list[dict[str, object]] | None = None
        self.listing_records: dict[str, tuple[date | None, date | None]] | None = None
        self.calendar_days: object = None
        self.calendar_source: str | None = None

    async def sync_with_diff(
        self, dicts: list[dict[str, object]], *, as_of: date
    ) -> object:
        self.synced = dicts
        return SimpleNamespace(new=0, updated=0, renamed=[], pending_delist=[], delisted=[])

    async def backfill_metadata_from_profiles(
        self, *, symbols: list[str] | None = None
    ) -> object:
        return SimpleNamespace(
            as_dict=lambda: {
                "profile_batch_available": False,
                "scoped": 0,
                "backfilled_list_date": 0,
                "backfilled_industry": 0,
                "backfilled_delist_date": 0,
                "missing_list_date": 0,
                "missing_industry": 0,
                "missing_delist_date": 0,
            }
        )

    async def backfill_listing_dates(
        self, records: dict[str, tuple[date | None, date | None]]
    ) -> dict[str, int]:
        self.listing_records = records
        return {
            "scoped": len(records),
            "backfilled_list_date": 0,
            "backfilled_delist_date": 0,
            "missing_list_date": 0,
            "missing_delist_date": 0,
        }


class _FakeSession:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _job_record() -> object:
    from finboard_backtest.background_jobs.contracts import JobRecord
    from finboard_shared.background_jobs import generate_background_job_id

    return JobRecord(
        job_id=generate_background_job_id(),
        kind="data_sync",
        queue="data",
        payload={},
        attempt=1,
        max_attempts=3,
        requested_by="test:395",
    )


_UNSET = object()


def _run_executor(
    monkeypatch: pytest.MonkeyPatch,
    discovered: list[InstrumentInfo],
    *,
    session_maker: Callable[[], _FakeSession],
    calendar_days: list[FuturesTradeCalendarDay] | None | object = _UNSET,
) -> dict[str, object]:
    """打桩 discovery / InstrumentRepository / TradeCalRepository 后同步驱动。

    ``calendar_days`` 缺省哨兵 = 不干预 ``_fetch_futures_trade_calendar``
    (走真实函数:无 token 环境具名跳过返回 None);传 list = 打桩返回;
    传 None = 打桩跳过。
    """
    captured: dict[str, object] = {}

    class _FakeDiscovery:
        async def discover_all(self) -> list[InstrumentInfo]:
            return discovered

    monkeypatch.setattr(
        "finboard_data.discovery.UniverseDiscovery", _FakeDiscovery
    )

    def _repo_factory(session: object) -> _FakeRepo:
        repo = _FakeRepo(session)
        captured["repo"] = repo
        return repo

    monkeypatch.setattr(
        "finboard_persistence.InstrumentRepository", _repo_factory
    )

    if calendar_days is not _UNSET:

        async def _fake_fetch(
            *, job_id: str
        ) -> list[FuturesTradeCalendarDay] | None:
            if calendar_days is None:
                return None
            assert isinstance(calendar_days, list)
            return calendar_days

        monkeypatch.setattr(
            "finboard_backtest.background_jobs.executors.data_sync"
            "._fetch_futures_trade_calendar",
            _fake_fetch,
        )

    class _FakeTradeCalRepo:
        def __init__(self, session: object) -> None:
            pass

        async def upsert_calendar_days(
            self, days: Sequence[object], *, source: str
        ) -> dict[str, int]:
            repo = captured["repo"]
            assert isinstance(repo, _FakeRepo)
            repo.calendar_days = days
            repo.calendar_source = source
            return {"received": len(days), "written": len(days), "trading_days": 1}

    monkeypatch.setattr(
        "finboard_persistence.TradeCalRepository", _FakeTradeCalRepo
    )

    executor = DataSyncExecutor(
        session_maker=session_maker,  # type: ignore[arg-type]
    )
    captured["result"] = asyncio.run(
        executor.execute(_job_record(), AsyncMock())  # type: ignore[arg-type]
    )
    return captured


def _session_maker_of(session: _FakeSession) -> Callable[[], _FakeSession]:
    return lambda: session


# ---- 期货合约 list_date / delist_date 回填(#395)--------------------------------


@pytest.mark.unit
def test_data_sync_backfills_futures_listing_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期货合约携带 fut_basic 上市/退市日 → 回填映射二元组;股票不在域。"""
    from finboard_shared.types import InstrumentType, ListingBoard, Market

    discovered = [
        InstrumentInfo(
            code="IF2612.CFFEX",
            name="IF2612",
            market=Market.FUTURE,
            instrument_type=InstrumentType.FUTURES,
            exchange="CFFEX",
            list_date=date(2026, 8, 24),
            delist_date=date(2026, 12, 18),
        ),
        # 主连:受控登记无结构化上市日,不进回填域(缺失可见)。
        InstrumentInfo(
            code="IF0.CFFEX",
            name="IF0",
            market=Market.FUTURE,
            instrument_type=InstrumentType.FUTURES,
            exchange="CFFEX",
        ),
        InstrumentInfo(
            code="600000.SH",
            name="浦发银行",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
            exchange="SSE",
            listing_board=ListingBoard.SSE_MAIN,
        ),
    ]
    session = _FakeSession(_FakeRepo(None))
    captured = _run_executor(
        monkeypatch,
        discovered,
        session_maker=_session_maker_of(session),
    )
    repo = captured["repo"]
    assert isinstance(repo, _FakeRepo)
    assert repo.listing_records == {
        "IF2612.CFFEX": (date(2026, 8, 24), date(2026, 12, 18))
    }


# ---- fut_trade_cal 尽力而为落库(#395)-------------------------------------------


def _cal_day(day: date, *, is_open: bool) -> FuturesTradeCalendarDay:
    observed = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
    return FuturesTradeCalendarDay(
        exchange="CFFEX",
        cal_date=day,
        is_open=is_open,
        pretrade_date=None,
        source="tushare",
        observed_at=observed,
        available_at=observed,
    )


@pytest.mark.unit
def test_data_sync_upserts_futures_trade_cal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日历拉到 → TradeCalRepository.upsert(tushare 源),完成语带交易日数。"""
    session = _FakeSession(_FakeRepo(None))
    captured = _run_executor(
        monkeypatch,
        [],
        session_maker=_session_maker_of(session),
        calendar_days=[_cal_day(date(2026, 9, 9), is_open=True)],
    )
    repo = captured["repo"]
    assert isinstance(repo, _FakeRepo)
    assert repo.calendar_source == "tushare"
    assert repo.calendar_days is not None
    result = captured["result"]
    assert isinstance(result, JobResult)
    assert result.status == "succeeded"


@pytest.mark.unit
def test_data_sync_calendar_skipped_is_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日历跳过(token 缺失 / 上游失败)→ 任务照常 succeeded,计数为 None。"""
    session = _FakeSession(_FakeRepo(None))
    captured = _run_executor(
        monkeypatch,
        [],
        session_maker=_session_maker_of(session),
        calendar_days=None,
    )
    repo = captured["repo"]
    assert isinstance(repo, _FakeRepo)
    assert repo.calendar_days is None
    assert repo.calendar_source is None
    result = captured["result"]
    assert isinstance(result, JobResult)
    assert result.status == "succeeded"


@pytest.mark.unit
async def test_fetch_futures_trade_calendar_token_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token 未配置 → 具名告警返回 None(尽力而为,不抛错)。"""
    monkeypatch.setenv("FINBOARD_TUSHARE_TOKEN", "")
    assert await _fetch_futures_trade_calendar(job_id="BJ-395") is None
