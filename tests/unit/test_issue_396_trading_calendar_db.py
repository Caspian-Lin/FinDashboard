"""issue #396:交易日历 DB 优先加载路径(``ensure_calendar_loaded``)。

顺序契约:进程缓存 → PG store → 空/过期回源 akshare(回写 store)→
exchange_calendars 兜底。全部失败缓存空集(与同步路径同语义);消费方
``trading_days`` 抛 :class:`TradingCalendarError` 收口。未安装 store 时
行为与历史一致(不走 DB)。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finboard_data.trading_calendar import (
    TradingCalendarError,
    ensure_calendar_loaded,
    install_calendar_store,
    reset_cache,
    trading_days,
)

pytestmark = pytest.mark.unit


class FakeStore:
    """进程内假存储;记录 save 调用与可配置失败。"""

    def __init__(
        self,
        days: set[date] | None = None,
        *,
        load_error: bool = False,
        save_error: bool = False,
    ) -> None:
        self.days = set(days) if days is not None else None
        self.load_error = load_error
        self.save_error = save_error
        self.saved: list[set[date]] = []

    async def load(self) -> set[date] | None:
        if self.load_error:
            raise RuntimeError("db down")
        return set(self.days) if self.days else None

    async def save(self, days: set[date]) -> None:
        if self.save_error:
            raise RuntimeError("db down")
        self.saved.append(set(days))
        self.days = set(days) if self.days is None else self.days | set(days)


@pytest.fixture(autouse=True)
def _clean_calendar_state():
    reset_cache()
    install_calendar_store(None)
    yield
    reset_cache()
    install_calendar_store(None)


def _akshare_days(monkeypatch: pytest.MonkeyPatch, upto: date) -> None:
    days = {upto - timedelta(days=i) for i in range(60)}
    days = {d for d in days if d.weekday() < 5}
    monkeypatch.setattr(
        "finboard_data.trading_calendar._fetch_trade_dates", lambda: days
    )


class TestEnsureCalendarLoaded:
    async def test_db_hit_serves_without_akshare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> set[date]:
            raise AssertionError("DB 命中时不应回源 akshare")

        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates", _fail
        )
        db_days = {date(2026, 7, 20), date(2026, 7, 21)}
        install_calendar_store(FakeStore(db_days))
        loaded = await ensure_calendar_loaded()
        assert loaded == db_days

    async def test_db_empty_backfills_from_akshare_and_saves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _akshare_days(monkeypatch, date.today())
        store = FakeStore(None)
        install_calendar_store(store)
        loaded = await ensure_calendar_loaded()
        assert loaded
        assert store.saved
        assert store.saved[0] == loaded

    async def test_db_load_failure_falls_back_to_akshare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _akshare_days(monkeypatch, date.today())
        store = FakeStore(None, load_error=True)
        install_calendar_store(store)
        loaded = await ensure_calendar_loaded()
        assert loaded
        # load 失败时 save 也应尝试(回写尽力而为)。
        assert store.saved

    async def test_akshare_failure_falls_back_to_exchange_calendars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> set[date]:
            raise RuntimeError("akshare down")

        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates", _fail
        )
        install_calendar_store(FakeStore(None))
        loaded = await ensure_calendar_loaded()
        # 本机 exchange_calendars 可用;无论是否可用,不得抛异常。
        assert isinstance(loaded, set)

    async def test_db_save_failure_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _akshare_days(monkeypatch, date.today())
        install_calendar_store(FakeStore(None, save_error=True))
        loaded = await ensure_calendar_loaded()
        assert loaded

    async def test_stale_db_refreshes_from_akshare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DB 最大日期早于今天(跨年未回源)→ 视为可能缺新区间,重拉合并。
        stale = {date(2024, 1, 2), date(2024, 1, 3)}
        fresh = {date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)}
        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates", lambda: fresh
        )
        store = FakeStore(stale)
        install_calendar_store(store)
        loaded = await ensure_calendar_loaded()
        assert loaded == fresh
        assert store.saved
        assert store.saved[0] == fresh

    async def test_no_store_installed_legacy_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _akshare_days(monkeypatch, date.today())
        loaded = await ensure_calendar_loaded()
        assert loaded

    async def test_all_sources_fail_cache_empty_and_trading_days_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> set[date]:
            raise RuntimeError("down")

        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates", _fail
        )
        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates_from_exchange_calendars",
            _fail,
        )
        install_calendar_store(FakeStore(None))
        loaded = await ensure_calendar_loaded()
        assert loaded == set()
        with pytest.raises(TradingCalendarError):
            trading_days(date(2026, 7, 1), date(2026, 7, 31))

    async def test_process_cache_reused_across_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"count": 0}

        def _fetch() -> set[date]:
            calls["count"] += 1
            return {date(2026, 7, 20)}

        monkeypatch.setattr(
            "finboard_data.trading_calendar._fetch_trade_dates", _fetch
        )
        install_calendar_store(FakeStore(None))
        first = await ensure_calendar_loaded()
        second = await ensure_calendar_loaded()
        assert first == second
        assert calls["count"] == 1
