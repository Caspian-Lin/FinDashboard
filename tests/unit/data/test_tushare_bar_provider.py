"""Tushare A 股日线与请求预算测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock

import pytest
import structlog
from structlog.testing import capture_logs

import finboard_data.tushare_bar_provider as tushare_bar_provider_module
from finboard_data import (
    HistoricalDataProvider,
    TushareBarProvider,
    TushareRequestBudget,
    TushareRequestLimitError,
)
from finboard_data.cache import make_symbol
from finboard_shared.models import Bar
from finboard_shared.types import BarPeriod


class NoopBudget:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


class FakeTushareBarClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.daily_rows: list[dict[str, object]] = [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20240103",
                "open": 20,
                "high": 22,
                "low": 19,
                "close": 21,
                "vol": 12,
                "amount": 34.5,
            },
            {
                "ts_code": "000001.SZ",
                "trade_date": "20240102",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 10,
                "amount": 20,
            },
        ]
        self.factor_rows: list[dict[str, object]] = [
            {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2},
            {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1},
        ]
        self.suspend_rows: list[dict[str, object]] = []

    def daily(self, **kwargs: str) -> object:
        self.calls.append(("daily", kwargs))
        return self.daily_rows

    def cb_daily(self, **kwargs: str) -> object:
        self.calls.append(("cb_daily", kwargs))
        return []

    def adj_factor(self, **kwargs: str) -> object:
        self.calls.append(("adj_factor", kwargs))
        return self.factor_rows

    def suspend_d(self, **kwargs: str) -> object:
        self.calls.append(("suspend_d", kwargs))
        return self.suspend_rows


def _cached_bar(
    day: date,
    *,
    source: str = "tushare",
    high: str = "11",
) -> Bar:
    symbol = make_symbol("000001.SZ")
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        open=Decimal("10"),
        high=Decimal(high),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("100"),
        amount=Decimal("1000"),
        source=source,
    )


def _provider(
    client: FakeTushareBarClient | None = None,
) -> tuple[TushareBarProvider, NoopBudget]:
    budget = NoopBudget()
    return (
        TushareBarProvider(
            client=client or FakeTushareBarClient(),
            budget=budget,
            use_cache=False,
            max_retries=0,
        ),
        budget,
    )


@pytest.mark.unit
def test_tushare_bar_provider_satisfies_historical_protocol() -> None:
    provider, _ = _provider()
    assert isinstance(provider, HistoricalDataProvider)
    assert "token" not in repr(provider)


@pytest.mark.unit
async def test_fetch_qfq_bars_normalizes_units_and_orders_rows() -> None:
    provider, budget = _provider()

    bars = await provider.fetch_bars(
        make_symbol("000001.SZ"),
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="qfq",
    )

    assert [bar.timestamp.date() for bar in bars] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert bars[0].close == Decimal("5.25")
    assert bars[1].close == Decimal("21")
    assert bars[0].volume == Decimal("1000")
    assert bars[0].amount == Decimal("20000")
    assert {bar.source for bar in bars} == {"tushare"}
    assert budget.calls == 2


@pytest.mark.unit
async def test_none_adjustment_uses_one_request() -> None:
    client = FakeTushareBarClient()
    provider, budget = _provider(client)

    bars = await provider.fetch_bars(
        make_symbol("000001.SZ"),
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="none",
    )

    assert bars[0].close == Decimal("10.5")
    assert [name for name, _ in client.calls] == ["daily"]
    assert budget.calls == 1


@pytest.mark.unit
async def test_missing_adjustment_factor_fails_closed() -> None:
    client = FakeTushareBarClient()
    client.factor_rows = client.factor_rows[:1]
    provider, _ = _provider(client)

    with pytest.raises(ValueError, match="缺少 2024-01-02 的复权因子"):
        await provider.fetch_bars(
            make_symbol("000001.SZ"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 3),
        )


@pytest.mark.unit
async def test_non_daily_period_is_rejected_without_request() -> None:
    provider, budget = _provider()

    with pytest.raises(ValueError, match="只支持 A 股日线"):
        await provider.fetch_bars(
            make_symbol("000001.SZ"),
            BarPeriod.M5,
            date(2024, 1, 2),
            date(2024, 1, 3),
        )
    assert budget.calls == 0


@pytest.mark.unit
async def test_suspension_events_distinguish_full_day_intraday_and_resumption() -> None:
    client = FakeTushareBarClient()
    client.suspend_rows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240103",
            "suspend_timing": None,
            "suspend_type": "S",
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240104",
            "suspend_timing": "09:30-10:30",
            "suspend_type": "S",
        },
        {
            "ts_code": "000001.SZ",
            "trade_date": "20240105",
            "suspend_timing": None,
            "suspend_type": "R",
        },
    ]
    provider, budget = _provider(client)

    events = await provider.fetch_suspension_events(
        make_symbol("000001.SZ"),
        date(2024, 1, 2),
        date(2024, 1, 5),
    )

    assert [event.event_type for event in events] == [
        "suspension_day",
        "intraday_suspension",
        "resumption",
    ]
    assert events[1].suspend_timing == "09:30-10:30"
    assert client.calls[-1] == (
        "suspend_d",
        {
            "ts_code": "000001.SZ",
            "start_date": "20240102",
            "end_date": "20240105",
            "fields": "ts_code,trade_date,suspend_timing,suspend_type",
        },
    )
    assert budget.calls == 1


@pytest.mark.unit
async def test_suspension_events_reject_wrong_symbol() -> None:
    client = FakeTushareBarClient()
    client.suspend_rows = [
        {
            "ts_code": "600000.SH",
            "trade_date": "20240103",
            "suspend_timing": None,
            "suspend_type": "S",
        }
    ]
    provider, _ = _provider(client)

    with pytest.raises(ValueError, match="非请求标的"):
        await provider.fetch_suspension_events(
            make_symbol("000001.SZ"),
            date(2024, 1, 2),
            date(2024, 1, 5),
        )


@pytest.mark.unit
async def test_daily_budget_persists_and_fails_before_overrun(tmp_path: Path) -> None:
    usage_file = tmp_path / "usage.json"
    budget = TushareRequestBudget(
        requests_per_minute=60_000_000,
        daily_request_limit=1,
        usage_file=usage_file,
        now=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )

    await budget.acquire()
    assert '"requests": 1' in usage_file.read_text(encoding="utf-8")
    with pytest.raises(TushareRequestLimitError, match="1/1"):
        await budget.acquire()


@pytest.mark.unit
async def test_budget_current_rpm_tracks_sliding_window(tmp_path: Path) -> None:
    budget = TushareRequestBudget(
        requests_per_minute=60_000_000,
        daily_request_limit=100_000,
        usage_file=tmp_path / "usage.json",
        now=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert budget.current_rpm == 0
    for _ in range(3):
        await budget.acquire()
    assert budget.current_rpm == 3


@pytest.mark.unit
async def test_budget_persists_before_every_remote_request(tmp_path: Path) -> None:
    """每个请求均单独写入预算文件,不使用批量额度预占。"""
    from unittest.mock import patch

    usage_file = tmp_path / "usage.json"
    budget = TushareRequestBudget(
        requests_per_minute=60_000_000,
        daily_request_limit=100,
        usage_file=usage_file,
        now=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    with patch.object(budget, "_write_usage", wraps=budget._write_usage) as write:
        await budget.acquire()
        await budget.acquire()

    assert write.call_count == 2
    assert json.loads(usage_file.read_text(encoding="utf-8"))["requests"] == 2


@pytest.mark.unit
async def test_batch_uses_sixteen_workers_for_200_slow_symbols() -> None:
    class SlowTushareBarClient:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.maximum = 0
            self.lock = Lock()
            self.saturated = Event()

        def cb_daily(self, **kwargs: str) -> object:
            return []

        def daily(self, **kwargs: str) -> object:
            with self.lock:
                self.calls += 1
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 16:
                    self.saturated.set()
            assert self.saturated.wait(timeout=5)
            with self.lock:
                self.active -= 1
            return [
                {
                    "ts_code": kwargs["ts_code"],
                    "trade_date": "20240102",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 10,
                    "amount": 20,
                }
            ]

        def adj_factor(self, **kwargs: str) -> object:
            raise AssertionError("none 复权不应请求 adj_factor")

        def suspend_d(self, **kwargs: str) -> object:
            return []

    client = SlowTushareBarClient()
    provider = TushareBarProvider(
        client=client,
        use_cache=False,
        budget=NoopBudget(),
        max_concurrency=16,
        max_retries=0,
    )
    symbols = [make_symbol(f"{index:06d}.SZ") for index in range(1, 201)]
    results = await provider.update_cache_batch(
        symbols,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 2),
        adjust="none",
    )

    assert len(results) == 200
    assert all(results.values())
    assert client.calls == 200
    assert client.maximum == 16


@pytest.mark.unit
async def test_cache_hit_does_not_call_tushare_for_same_source(tmp_path: Path) -> None:
    provider = TushareBarProvider(
        client=FakeTushareBarClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000001.SZ")
    await provider._cache.write(
        symbol,
        BarPeriod.D1,
        "none",
        [_cached_bar(date(2024, 1, 2)), _cached_bar(date(2024, 1, 3))],
    )

    from unittest.mock import AsyncMock

    fetch = AsyncMock()
    provider._fetch_from_akshare = fetch  # type: ignore[method-assign]
    result = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="none",
    )

    assert result is True
    fetch.assert_not_awaited()


@pytest.mark.unit
async def test_incremental_cache_fetch_starts_after_last_cached_date(tmp_path: Path) -> None:
    provider = TushareBarProvider(
        client=FakeTushareBarClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000001.SZ")
    await provider._cache.write(
        symbol,
        BarPeriod.D1,
        "none",
        [_cached_bar(date(2024, 1, 2)), _cached_bar(date(2024, 1, 3))],
    )

    from unittest.mock import AsyncMock

    fetch = AsyncMock(return_value=[_cached_bar(date(2024, 1, 4)), _cached_bar(date(2024, 1, 5))])
    provider._fetch_from_akshare = fetch  # type: ignore[method-assign]
    result = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 5),
        adjust="none",
    )

    assert result is True
    fetch.assert_awaited_once()
    assert fetch.await_args is not None
    assert fetch.await_args.args[2:] == (date(2024, 1, 4), date(2024, 1, 5), "none")


@pytest.mark.unit
async def test_successful_empty_tail_is_cached_as_covered(tmp_path: Path) -> None:
    provider = TushareBarProvider(
        client=FakeTushareBarClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000001.SZ")
    await provider._cache.write(
        symbol,
        BarPeriod.D1,
        "none",
        [
            _cached_bar(date(2024, 1, 2)),
            _cached_bar(date(2024, 1, 3)),
        ],
    )

    from unittest.mock import AsyncMock

    fetch = AsyncMock(return_value=[])
    provider._fetch_from_akshare = fetch  # type: ignore[method-assign]
    statuses: list[str] = []
    first = await provider.update_cache(
        symbol, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 5), adjust="none"
    )
    second = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 5),
        adjust="none",
        on_status=statuses.append,
    )

    assert first is True
    assert second is True
    fetch.assert_awaited_once()
    assert fetch.await_args is not None
    assert fetch.await_args.args[2:] == (date(2024, 1, 4), date(2024, 1, 5), "none")
    assert statuses == ["checking_cache", "cache_hit"]
    sidecar = tmp_path / "000001.SZ_1d_none.parquet.meta.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["covered_ranges"] == [["2024-01-04", "2024-01-05"]]


@pytest.mark.unit
async def test_internal_no_bar_day_does_not_cause_permanent_cache_miss(
    tmp_path: Path,
) -> None:
    """停牌等合法无 Bar 日期不能仅凭交易日历被反复重拉。"""
    provider = TushareBarProvider(
        client=FakeTushareBarClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000001.SZ")
    await provider._cache.write(
        symbol,
        BarPeriod.D1,
        "none",
        [
            _cached_bar(date(2024, 1, 2)),
            _cached_bar(date(2024, 1, 3)),
            _cached_bar(date(2024, 1, 5)),
        ],
    )

    from unittest.mock import AsyncMock

    fetch = AsyncMock()
    provider._fetch_from_akshare = fetch  # type: ignore[method-assign]
    result = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 5),
        adjust="none",
    )

    assert result is True
    fetch.assert_not_awaited()


@pytest.mark.unit
async def test_leading_pre_listing_range_is_only_queried_once(tmp_path: Path) -> None:
    """上市前无数据区间成功查询后,第二次必须直接命中缓存。"""
    provider = TushareBarProvider(
        client=FakeTushareBarClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000001.SZ")

    await provider._cache.write(
        symbol,
        BarPeriod.D1,
        "none",
        [_cached_bar(date(2024, 1, 4)), _cached_bar(date(2024, 1, 5))],
    )
    from unittest.mock import AsyncMock

    fetch = AsyncMock(return_value=[])
    provider._fetch_from_akshare = fetch  # type: ignore[method-assign]
    first = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 5),
        adjust="none",
    )
    second = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 5),
        adjust="none",
    )

    assert first is True
    assert second is True
    fetch.assert_awaited_once()
    assert fetch.await_args is not None
    assert fetch.await_args.args[2:] == (date(2024, 1, 2), date(2024, 1, 3), "none")


# --------------------------------------------------------------------------- 异源缓存策略(issue #257)


def _foreign_bar(day: date, close: str = "4.0") -> Bar:
    return replace(_cached_bar(day, source="akshare"), close=Decimal(close))


class TestForeignCacheStrategy:
    """tushare provider 对 akshare 等异源缓存的读取策略(issue #257)。"""

    def _provider(self, tmp_path: Path, client: FakeTushareBarClient | None = None):
        budget = NoopBudget()
        return (
            TushareBarProvider(
                client=client or FakeTushareBarClient(),
                budget=budget,
                cache_dir=tmp_path,
                max_retries=0,
            ),
            budget,
        )

    @pytest.mark.unit
    async def test_full_foreign_coverage_serves_read_through(self, tmp_path: Path) -> None:
        """异源缓存完整覆盖请求区间:直接返回缓存,零 tushare 请求。"""
        provider, budget = self._provider(tmp_path)
        assert provider._cache is not None
        symbol = make_symbol("510300.SH")
        foreign = [_foreign_bar(date(2024, 1, 2)), _foreign_bar(date(2024, 1, 3))]
        await provider._cache.write(symbol, BarPeriod.D1, "qfq", foreign)

        from unittest.mock import AsyncMock

        fetch = AsyncMock()
        provider._fetch_from_akshare = fetch
        result = await provider.fetch_bars(
            symbol, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 3), adjust="qfq"
        )

        fetch.assert_not_awaited()
        assert budget.calls == 0
        assert [bar.source for bar in result] == ["akshare", "akshare"]
        assert [bar.timestamp.date() for bar in result] == [
            date(2024, 1, 2),
            date(2024, 1, 3),
        ]

    @pytest.mark.unit
    async def test_update_cache_full_foreign_coverage_hits_cache(self, tmp_path: Path) -> None:
        provider, budget = self._provider(tmp_path)
        assert provider._cache is not None
        symbol = make_symbol("510300.SH")
        await provider._cache.write(
            symbol,
            BarPeriod.D1,
            "qfq",
            [_foreign_bar(date(2024, 1, 2)), _foreign_bar(date(2024, 1, 3))],
        )

        from unittest.mock import AsyncMock

        fetch = AsyncMock()
        provider._fetch_from_akshare = fetch
        statuses: list[str] = []
        result = await provider.update_cache(
            symbol,
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 3),
            adjust="qfq",
            on_status=statuses.append,
        )

        assert result is True
        fetch.assert_not_awaited()
        assert budget.calls == 0
        assert statuses == ["checking_cache", "cache_hit"]

    @pytest.mark.unit
    async def test_foreign_cache_falls_back_when_tushare_empty(self, tmp_path: Path) -> None:
        """缺口区间 tushare 拉不到(股票上游为空):具名回退返回异源缓存,不改写缓存。

        #341 起 ETF 在 provider 层 fail-visible 拒绝(不再静默空结果回退),
        空拉回退语义改用上游为空的股票标的锁定。
        """
        client = FakeTushareBarClient()
        client.daily_rows = []
        client.factor_rows = []
        provider, _ = self._provider(tmp_path, client)
        assert provider._cache is not None
        symbol = make_symbol("000001.SZ")
        foreign = [_foreign_bar(date(2024, 1, 2)), _foreign_bar(date(2024, 1, 3))]
        await provider._cache.write(symbol, BarPeriod.D1, "qfq", foreign)

        result = await provider.fetch_bars(
            symbol, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 10), adjust="qfq"
        )

        # tushare 拉不到时回退异源已缓存区间(1/2-1/3),不再静默丢弃
        assert [bar.timestamp.date() for bar in result] == [
            date(2024, 1, 2),
            date(2024, 1, 3),
        ]
        assert {bar.source for bar in result} == {"akshare"}
        # 缓存文件保持异源原样,不被 tushare 空结果污染
        metadata = await provider._cache.metadata_for(symbol, BarPeriod.D1, "qfq")
        assert metadata is not None
        assert metadata.source == "akshare"

    @pytest.mark.unit
    async def test_foreign_cache_rebuilds_when_tushare_has_bars(self, tmp_path: Path) -> None:
        """缺口区间 tushare 拉得到(股票):保持既有重建语义,写回纯 tushare 缓存。"""
        provider, _ = self._provider(tmp_path)
        assert provider._cache is not None
        symbol = make_symbol("000001.SZ")
        await provider._cache.write(
            symbol,
            BarPeriod.D1,
            "none",
            [_foreign_bar(date(2024, 1, 2))],
        )

        result = await provider.fetch_bars(
            symbol, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 3), adjust="none"
        )

        assert [bar.timestamp.date() for bar in result] == [
            date(2024, 1, 2),
            date(2024, 1, 3),
        ]
        assert {bar.source for bar in result} == {"tushare"}
        metadata = await provider._cache.metadata_for(symbol, BarPeriod.D1, "none")
        assert metadata is not None
        assert metadata.source == "tushare"

    @pytest.mark.unit
    async def test_no_cache_full_range_still_fetched(self, tmp_path: Path) -> None:
        """无缓存时全量拉取行为不回归。"""
        provider, budget = self._provider(tmp_path)
        result = await provider.fetch_bars(
            make_symbol("000001.SZ"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 3),
            adjust="qfq",
        )
        assert len(result) == 2
        assert budget.calls > 0


# ---------------------------------------------------------------------------
# #265:可转债日线(cb_daily 专属接口,无复权,1 手 = 10 张)
# ---------------------------------------------------------------------------


class FakeConvertibleTushareClient(FakeTushareBarClient):
    """cb_daily 桩:记录调用并可断言股票接口未被触碰。"""

    def __init__(self) -> None:
        super().__init__()
        self.cb_daily_rows: list[dict[str, object]] = [
            {
                "ts_code": "113050.SH",
                "trade_date": "20240103",
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101.5,
                "vol": 30,
                "amount": 3045.0,
            },
            {
                "ts_code": "113050.SH",
                "trade_date": "20240102",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "vol": 20,
                "amount": 2010.0,
            },
        ]

    def cb_daily(self, **kwargs: str) -> object:
        self.calls.append(("cb_daily", kwargs))
        return self.cb_daily_rows


@pytest.mark.unit
async def test_convertible_daily_uses_cb_daily_without_adjustment() -> None:
    """转债按代码规则分流 cb_daily:不调 daily/adj_factor,原始价落盘。"""
    client = FakeConvertibleTushareClient()
    provider, budget = _provider(client)

    bars = await provider.fetch_bars(
        make_symbol("113050.SH"),
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="qfq",
    )

    assert [bar.timestamp.date() for bar in bars] == [date(2024, 1, 2), date(2024, 1, 3)]
    # 无复权:收盘价 = 上游原始价。
    assert bars[0].close == Decimal("100.5")
    assert bars[1].close == Decimal("101.5")
    # 转债 1 手 = 10 张:vol 单位换算与股票(x100)不同。
    assert bars[0].volume == Decimal("200")
    assert bars[0].amount == Decimal("2010000")
    assert {bar.source for bar in bars} == {"tushare"}
    # 只调 cb_daily,不碰股票 daily / 复权因子接口;两天同 chunk 一次取回。
    assert [name for name, _ in client.calls] == ["cb_daily"]
    assert client.calls[0][1]["ts_code"] == "113050.SH"
    assert budget.calls == 1


@pytest.mark.unit
async def test_convertible_daily_cache_key_follows_requested_adjust(
    tmp_path: Path,
) -> None:
    """缓存键沿用请求 adjust(qfq 键存在但语义为 no-op),发布口径与下载键一致。"""
    provider = TushareBarProvider(
        client=FakeConvertibleTushareClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("113050.SH")

    ok = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="qfq",
    )
    assert ok is True
    metadata = await provider._cache.metadata_for(symbol, BarPeriod.D1, "qfq")
    assert metadata is not None
    assert metadata.bar_count == 2
    assert metadata.source == "tushare"
    cached = await provider._cache.read(symbol, BarPeriod.D1, "qfq")
    assert [bar.close for bar in cached] == [Decimal("100.5"), Decimal("101.5")]


# ---------------------------------------------------------------------------
# #341:指数日线(index_daily 专属接口,无复权)+ ETF fail-visible
# ---------------------------------------------------------------------------


class FakeIndexTushareClient(FakeTushareBarClient):
    """index_daily 桩:记录调用并可断言股票/复权接口未被触碰。"""

    def __init__(self) -> None:
        super().__init__()
        self.index_daily_rows: list[dict[str, object]] = [
            {
                "ts_code": "000852.SH",
                "trade_date": "20240103",
                "open": 7000,
                "high": 7100,
                "low": 6950,
                "close": 7050.5,
                "vol": 300,
                "amount": 492508.5,
            },
            {
                "ts_code": "000852.SH",
                "trade_date": "20240102",
                "open": 6900,
                "high": 7020,
                "low": 6880,
                "close": 6980.25,
                "vol": 280,
                "amount": 460001.0,
            },
        ]

    def index_daily(self, **kwargs: str) -> object:
        self.calls.append(("index_daily", kwargs))
        return self.index_daily_rows


@pytest.mark.unit
async def test_index_daily_uses_index_daily_without_adjustment() -> None:
    """指数按代码规则分流 index_daily:不调 daily/adj_factor,原始指数点落盘。"""
    client = FakeIndexTushareClient()
    provider, budget = _provider(client)

    bars = await provider.fetch_bars(
        make_symbol("000852.SH"),
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="qfq",
    )

    assert [bar.timestamp.date() for bar in bars] == [date(2024, 1, 2), date(2024, 1, 3)]
    # 无复权:收盘价 = 上游原始指数点(qfq 请求下不做因子换算)。
    assert bars[0].close == Decimal("6980.25")
    assert bars[1].close == Decimal("7050.5")
    # vol/amount 单位与股票 daily 相同(手 x100 / 千元 x1000)。
    assert bars[0].volume == Decimal("28000")
    assert bars[0].amount == Decimal("460001000")
    assert {bar.source for bar in bars} == {"tushare"}
    # 只调 index_daily,不碰股票 daily / 复权因子接口;两天同 chunk 一次取回。
    assert [name for name, _ in client.calls] == ["index_daily"]
    assert client.calls[0][1]["ts_code"] == "000852.SH"
    assert budget.calls == 1


@pytest.mark.unit
async def test_index_daily_cache_key_follows_requested_adjust(tmp_path: Path) -> None:
    """缓存键沿用请求 adjust(qfq 键存在但语义为 no-op),与发布口径一致。"""
    provider = TushareBarProvider(
        client=FakeIndexTushareClient(),
        cache_dir=tmp_path,
        max_retries=0,
    )
    assert provider._cache is not None
    symbol = make_symbol("000852.SH")

    ok = await provider.update_cache(
        symbol,
        BarPeriod.D1,
        date(2024, 1, 2),
        date(2024, 1, 3),
        adjust="qfq",
    )
    assert ok is True
    metadata = await provider._cache.metadata_for(symbol, BarPeriod.D1, "qfq")
    assert metadata is not None
    assert metadata.bar_count == 2
    assert metadata.source == "tushare"
    cached = await provider._cache.read(symbol, BarPeriod.D1, "qfq")
    assert [bar.close for bar in cached] == [Decimal("6980.25"), Decimal("7050.5")]


@pytest.mark.unit
async def test_etf_code_fails_visible_instead_of_silent_empty() -> None:
    """ETF 走股票 daily 会静默返回空(#341):fail-visible 指路 akshare。"""
    client = FakeIndexTushareClient()
    provider, _ = _provider(client)

    with pytest.raises(ValueError, match="tushare 源暂不提供 ETF 行情"):
        await provider.fetch_bars(
            make_symbol("510300.SH"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 3),
            adjust="qfq",
        )
    # 未触碰任何行情接口。
    assert client.calls == []


# ---------------------------------------------------------------------------
# #346:指数基日行 open/high/low 非有限值单行跳过(仅 index_daily 路径)
# ---------------------------------------------------------------------------


@pytest.fixture
def _unfiltered_structlog_for_provider():
    """隔离 ``setup_logging`` 对 structlog 的全局污染(#301/#315 同款)。

    CI 从仓库根跑全量(集成测试按字母序先于 unit),任何先行的测试调用
    ``setup_logging``(级别过滤 + ``cache_logger_on_first_use=True``)后,
    tushare_bar_provider 的模块 logger 已缓存,``capture_logs`` 永远抓空。
    本节断言具名 warning,测试期重置为不缓存并重建模块 logger,结束恢复。
    """
    saved_config = structlog.get_config()
    saved_logger = tushare_bar_provider_module.logger
    structlog.reset_defaults()
    structlog.configure(cache_logger_on_first_use=False)
    tushare_bar_provider_module.logger = structlog.get_logger(
        "finboard_data.tushare_bar_provider"
    )
    yield
    tushare_bar_provider_module.logger = saved_logger
    structlog.configure(**saved_config)


def _index_row(
    trade_date: str,
    *,
    open_: object = 1000,
    high: object = 1010,
    low: object = 990,
    close: object = 1005.5,
) -> dict[str, object]:
    return {
        "ts_code": "000688.SH",
        "trade_date": trade_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "vol": 200,
        "amount": 300000.0,
    }


@pytest.mark.unit
@pytest.mark.usefixtures("_unfiltered_structlog_for_provider")
async def test_index_base_day_nonfinite_ohlc_row_is_skipped_with_named_warning() -> None:
    """基日坏行(open/high/low=NaN、close 正常)单行跳过,好行照常落盘。"""
    client = FakeIndexTushareClient()
    client.index_daily_rows = [
        # 基日行(实测 000688.SH=2019-12-31、899050.BJ=2022-04-29 同形态):
        # close 正常,open/high/low 全 NaN。
        _index_row(
            "20191231",
            open_=float("nan"),
            high=float("nan"),
            low=float("nan"),
            close=988.5,
        ),
        _index_row("20200102"),
        # 部分 NaN(仅 high):同样属于「OHLC 含非有限值」的坏行。
        _index_row("20200103", high=float("nan")),
    ]
    provider, _ = _provider(client)

    with capture_logs() as logs:
        bars = await provider.fetch_bars(
            make_symbol("000688.SH"),
            BarPeriod.D1,
            date(2019, 12, 31),
            date(2020, 1, 3),
            adjust="qfq",
        )

    # 两根坏行被跳过,好行照常落盘(无复权,qfq 请求下原始指数点直落)。
    assert [bar.timestamp.date() for bar in bars] == [date(2020, 1, 2)]
    assert bars[0].close == Decimal("1005.5")
    assert bars[0].volume == Decimal("20000")
    assert {bar.source for bar in bars} == {"tushare"}
    # 具名 warning 带 symbol 与跳过日期列表。
    warnings = [
        entry
        for entry in logs
        if entry.get("event") == "tushare.index_row_skipped_nonfinite_ohlc"
    ]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["symbol"] == "000688.SH"
    assert warnings[0]["skipped_dates"] == ["2019-12-31", "2020-01-03"]
    assert warnings[0]["skipped_count"] == 2
    assert warnings[0]["total_rows"] == 3


@pytest.mark.unit
@pytest.mark.parametrize("bad_close", [float("nan"), 0])
@pytest.mark.usefixtures("_unfiltered_structlog_for_provider")
async def test_index_close_bad_row_still_rejects_whole_segment(bad_close: object) -> None:
    """index 路径 close 非有限或 ≤ 0 仍整段拒绝(fail-visible 不变)。"""
    client = FakeIndexTushareClient()
    client.index_daily_rows = [
        _index_row("20200102"),
        _index_row("20200103", close=bad_close),
    ]
    provider, _ = _provider(client)

    with capture_logs() as logs, pytest.raises(ValueError, match=r"close|无效价格"):
        await provider.fetch_bars(
            make_symbol("000688.SH"),
            BarPeriod.D1,
            date(2020, 1, 2),
            date(2020, 1, 3),
            adjust="qfq",
        )

    # 整段拒绝不是单行跳过:不得发出跳过 warning。
    assert not [
        entry
        for entry in logs
        if entry.get("event") == "tushare.index_row_skipped_nonfinite_ohlc"
    ]


@pytest.mark.unit
@pytest.mark.usefixtures("_unfiltered_structlog_for_provider")
async def test_stock_single_nonfinite_ohlc_row_still_rejects_whole_segment() -> None:
    """股票 / 转债口径回归:一行坏仍整段拒,lenient_ohlc 不外溢。"""
    client = FakeTushareBarClient()
    client.daily_rows[0]["open"] = float("nan")
    provider, _ = _provider(client)

    with capture_logs() as logs, pytest.raises(ValueError, match="字段 open 不是有限数字"):
        await provider.fetch_bars(
            make_symbol("000001.SZ"),
            BarPeriod.D1,
            date(2024, 1, 2),
            date(2024, 1, 3),
            adjust="none",
        )

    assert not [
        entry
        for entry in logs
        if entry.get("event") == "tushare.index_row_skipped_nonfinite_ohlc"
    ]
