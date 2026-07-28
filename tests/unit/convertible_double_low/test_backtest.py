"""可转债双低策略回测引擎测试(issue #63)。"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.convertible_double_low.backtest import (
    run_backtest,
)
from finboard_backtest.convertible_double_low.config import (
    ConvertibleDoubleLowConfig,
    RebalanceFrequency,
)
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_shared.instruments import ConvertibleMetadata, Instrument
from finboard_shared.types import InstrumentType, ListingStatus, Market


def _make_snap(
    code: str,
    dt: date,
    close: Decimal,
    premium: Decimal = Decimal("0.10"),
    volume: Decimal = Decimal("1000000"),
    issuer: str = "600000.SH",
    days_to_mat: int = 365,
) -> ConvertibleSnapshot:
    return ConvertibleSnapshot(
        instrument=Instrument(
            code=code,
            name=f"测试{code}",
            market=Market.A_SHARE,
            instrument_type=InstrumentType.CONVERTIBLE,
            list_date=date(2020, 1, 1),
            status=ListingStatus.ACTIVE,
        ),
        metadata=ConvertibleMetadata(
            underlying_stock_code=issuer,
            conversion_price=Decimal("10"),
        ),
        as_of=dt,
        close=close,
        volume=volume,
        amount=close * volume,
        conversion_premium=premium,
        conversion_price=Decimal("10"),
        conversion_value=close / (Decimal("1") + premium),
        ytm=Decimal("-0.02"),
        days_to_maturity=days_to_mat,
        remaining_size=Decimal("100000000"),
        avg_amount_20d=close * volume,
    )


def _gen_dates(n: int, start: date | None = None) -> list[date]:
    if start is None:
        start = date(2023, 1, 2)
    return [start + timedelta(days=i) for i in range(n)]


def _build_single_bond_series(
    code: str,
    prices: list[Decimal],
    start: date | None = None,
) -> tuple[list[date], dict[date, list[ConvertibleSnapshot]]]:
    dates = _gen_dates(len(prices), start)
    by_date: dict[date, list[ConvertibleSnapshot]] = {}
    for dt, px in zip(dates, prices, strict=True):
        by_date[dt] = [_make_snap(code, dt, px)]
    return dates, by_date


def _build_multi_bond_series(
    bonds: dict[str, list[Decimal]],
    start: date | None = None,
) -> tuple[list[date], dict[date, list[ConvertibleSnapshot]]]:
    n = max(len(p) for p in bonds.values())
    dates = _gen_dates(n, start)
    by_date: dict[date, list[ConvertibleSnapshot]] = {dt: [] for dt in dates}
    for code, prices in bonds.items():
        for i, px in enumerate(prices):
            by_date[dates[i]].append(_make_snap(code, dates[i], px))
    return dates, by_date


def _next_opens_from_snapshots(
    dates: list[date],
    by_date: dict[date, list[ConvertibleSnapshot]],
    next_close: dict[str, list[Decimal]],
) -> tuple[dict[date, dict[str, Decimal]], dict[date, dict[str, Decimal]]]:
    opens: dict[date, dict[str, Decimal]] = {}
    vols: dict[date, dict[str, Decimal]] = {}
    for i, dt in enumerate(dates):
        if i + 1 < len(dates):
            opens[dt] = {s.code: next_close[s.code][i + 1] for s in by_date[dt] if s.code in next_close}
            vols[dt] = {s.code: s.volume for s in by_date[dt]}
    return opens, vols


class TestRunBacktest:
    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            run_backtest({}, {}, {}, {}, ConvertibleDoubleLowConfig())

    def test_single_bond_buys_and_holds(self) -> None:
        prices = [Decimal("105")] * 50
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(
            capital=Decimal("100000"),
            top_n=1,
            rebalance_frequency=RebalanceFrequency.MONTHLY,
        )
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.trades) > 0
        assert any(t.side == "buy" for t in result.trades)

    def test_bond_price_increase_produces_profit(self) -> None:
        prices = [Decimal("101")] * 30 + [Decimal("110")] * 30
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert result.final_equity > Decimal("100000")

    def test_no_trades_when_all_filtered(self) -> None:
        prices = [Decimal("200")] * 40
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(price_max=Decimal("130"))
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.trades) == 0

    def test_rebalance_dates_recorded(self) -> None:
        prices = [Decimal("105")] * 60
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(top_n=1, rebalance_frequency=RebalanceFrequency.BIWEEKLY)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.rebalance_dates) >= 3

    def test_cost_breakdown_tracked(self) -> None:
        prices = [Decimal("105")] * 50
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert result.cost_breakdown["commission"] > 0
        assert result.cost_breakdown["stamp_tax"] == 0

    def test_rank_exit_triggers_sell(self) -> None:
        bonds = {
            "AAA": [Decimal("101")] * 25 + [Decimal("120")] * 25,
            "BBB": [Decimal("110")] * 25 + [Decimal("102")] * 25,
        }
        dates, by_date = _build_multi_bond_series(bonds)
        next_opens = {}
        next_vols = {}
        for i, dt in enumerate(dates):
            n = i + 1 if i + 1 < len(dates) else i
            opens = {}
            vols = {}
            for code in bonds:
                opens[code] = bonds[code][n]
                vols[code] = Decimal("1000000")
            next_opens[dt] = opens
            next_vols[dt] = vols
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        sell_codes = {t.code for t in result.trades if t.side == "sell"}
        assert len(sell_codes) >= 1

    def test_issuer_concentration_limit(self) -> None:
        bonds_a = {
            "AAA": [Decimal("101")] * 50,
            "BBB": [Decimal("102")] * 50,
        }
        dates, by_date = _build_multi_bond_series(bonds_a)
        next_opens = {}
        next_vols = {}
        for i, dt in enumerate(dates):
            n = min(i + 1, len(dates) - 1)
            next_opens[dt] = {code: bonds_a[code][n] for code in bonds_a}
            next_vols[dt] = {code: Decimal("1000000") for code in bonds_a}
        cfg = ConvertibleDoubleLowConfig(
            capital=Decimal("100000"),
            top_n=2,
            max_weight_per_issuer=Decimal("0.10"),
        )
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.trades) > 0

    def test_equity_curve_populated(self) -> None:
        prices = [Decimal("105")] * 50
        dates, by_date = _build_single_bond_series("AAA", prices)
        next_opens = {dt: {"AAA": prices[min(i + 1, len(prices) - 1)]} for i, dt in enumerate(dates)}
        next_vols = {dt: {"AAA": Decimal("1000000")} for dt in dates}
        cfg = ConvertibleDoubleLowConfig(top_n=1)
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.equity_curve) > 0

    def test_holdings_log_populated_on_exit(self) -> None:
        bonds = {
            "AAA": [Decimal("101")] * 25 + [Decimal("150")] * 25,
        }
        dates, by_date = _build_multi_bond_series(bonds)
        next_opens = {}
        next_vols = {}
        for i, dt in enumerate(dates):
            n = min(i + 1, len(dates) - 1)
            next_opens[dt] = {code: bonds[code][n] for code in bonds}
            next_vols[dt] = {code: Decimal("1000000") for code in bonds}
        cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"), top_n=1, price_max=Decimal("140"))
        result = run_backtest(by_date, next_opens, next_vols, {}, cfg)
        assert len(result.holdings) >= 1
