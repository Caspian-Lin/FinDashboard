"""研究市场读取以冻结 manifest 为权威,期货后缀与日历接线回归。"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import structlog.testing

from finboard_backtest.research_run import signal_engine
from finboard_backtest.research_run.frozen_loader import _market_from_value
from finboard_backtest.research_run.signal_engine import (
    _earliest_feasible_decision_start,
    _load_benchmark_curve,
    _load_price_series,
    _market_close_map,
    _release_trading_days,
    _symbol_from_release,
    build_daily_equity_curve,
)
from finboard_shared.types import Market
from tests.unit.research_run.test_signal_engine import (
    _manifest,
    _spec,
    _StubInstrument,
    _StubProvider,
    _StubRelease,
)


@pytest.mark.parametrize("suffix", ["CFFEX", "SHFE", "DCE", "CZCE", "INE", "GFEX"])
def test_future_suffix_case_insensitive(suffix: str) -> None:
    assert _market_from_value(f"IM0.{suffix}") is Market.FUTURE
    assert _market_from_value(f"im0.{suffix.lower()}") is Market.FUTURE
    assert _market_from_value("future") is Market.FUTURE
    assert _market_from_value("600001.SH") is Market.A_SHARE


def test_missing_manifest_symbol_uses_suffix_fallback() -> None:
    provider = _provider()
    assert _symbol_from_release(provider, "IF0.CFFEX").market is Market.FUTURE
    assert _symbol_from_release(provider, "600099.SH").market is Market.A_SHARE


def _provider(code: str = "IM0.CFFEX") -> Any:
    # 用股票形状代码证明读取权威 market,单补期货后缀不足以通过本测试。
    instruments = (
        _StubInstrument("600001.SH"),
        _StubInstrument(code, market=Market.FUTURE),
    )
    return _StubProvider(
        _StubRelease("r1", instruments, start_date=date(2024, 1, 2), end_date=date(2024, 1, 4)),
        {
            code: {date(2024, 1, 2): Decimal("10"), date(2024, 1, 4): Decimal("12")},
            "600001.SH": {date(2024, 1, 3): Decimal("9")},
        },
    )


def _enforce_market(provider: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("fetch_bars", "fetch_point_in_time_bars"):
        original = getattr(provider, name)

        def wrap(method: Any) -> Any:
            async def checked(symbol: Any, *args: Any, **kwargs: Any) -> Any:
                expected = next(
                    item.market for item in provider.release.instruments if item.code == symbol.code
                )
                assert symbol.market is expected
                return await method(symbol, *args, **kwargs)

            return checked

        monkeypatch.setattr(provider, name, wrap(original))


@pytest.fixture
def signal_logger(monkeypatch: pytest.MonkeyPatch) -> structlog.testing.CapturingLogger:
    logger = structlog.testing.CapturingLogger()
    monkeypatch.setattr(signal_engine, "logger", logger)
    return logger


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["IM0.CFFEX", "600099.SH"])
async def test_calendar_benchmark_and_prices_use_manifest(
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    signal_logger: structlog.testing.CapturingLogger,
) -> None:
    provider = _provider(code)
    _enforce_market(provider, monkeypatch)
    manifest = replace(_manifest(_spec()), benchmark_config={"symbol": code})
    days = await _release_trading_days(provider)
    curve = await _load_benchmark_curve(manifest, lambda _: provider)
    assert days == [date(2024, 1, day) for day in (2, 3, 4)]
    assert curve[-1][1] == manifest.initial_capital * Decimal("1.2")
    assert not any(
        log.args[0]
        in {
            "research_run.trading_calendar_instrument_unreadable",
            "research_run.benchmark_release_unavailable",
        }
        for log in signal_logger.calls
    )
    prices = await _load_price_series(provider, [code], datetime(2024, 1, 4, 17, tzinfo=UTC))
    assert prices[code] == [10.0, 12.0]
    closes = await _market_close_map(provider, [code])
    assert closes[code][date(2024, 1, 4)] == Decimal("12")


@pytest.mark.asyncio
async def test_future_only_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    provider.release.instruments = provider.release.instruments[1:]
    _enforce_market(provider, monkeypatch)
    assert await _release_trading_days(provider) == [date(2024, 1, day) for day in (2, 4)]


@pytest.mark.asyncio
async def test_benchmark_missing_keeps_named_warning(
    signal_logger: structlog.testing.CapturingLogger,
) -> None:
    provider = _provider()
    manifest = replace(_manifest(_spec()), benchmark_config={"symbol": "MISSING.CFFEX"})

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("发布无该标的")

    provider.fetch_point_in_time_bars = fail
    assert await _load_benchmark_curve(manifest, lambda _: provider) == ()
    assert any(
        log.args[0] == "research_run.benchmark_release_unavailable" for log in signal_logger.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("db_available", [True, False])
async def test_equity_and_earliest_start_use_db_calendar(db_available: bool) -> None:
    from finboard_backtest.factor_lab import DEFAULT_MOMENTUM_LOOKBACK

    provider = _provider()
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(40)]
    provider.release.end_date = days[-1]

    async def loader() -> list[date] | None:
        return days if db_available else None

    points = await build_daily_equity_curve(
        provider,
        _manifest(_spec()),
        (),
        trading_days_loader=loader,
    )
    expected = days if db_available else [date(2024, 1, day) for day in (2, 3, 4)]
    assert [point.trade_date for point in points] == expected
    earliest = await _earliest_feasible_decision_start(provider, trading_days_loader=loader)
    assert earliest == (days[DEFAULT_MOMENTUM_LOOKBACK] if db_available else None)
