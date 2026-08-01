"""Tushare A 股日线与请求预算测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_data import (
    HistoricalDataProvider,
    TushareBarProvider,
    TushareRequestBudget,
    TushareRequestLimitError,
)
from finboard_data.cache import make_symbol
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

    def adj_factor(self, **kwargs: str) -> object:
        self.calls.append(("adj_factor", kwargs))
        return self.factor_rows

    def suspend_d(self, **kwargs: str) -> object:
        self.calls.append(("suspend_d", kwargs))
        return self.suspend_rows


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
