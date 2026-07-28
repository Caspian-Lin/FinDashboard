"""可转债双低策略归因分析测试(issue #63)。"""

from datetime import date, timedelta
from decimal import Decimal

from finboard_backtest.convertible_double_low.analysis import (
    CAPITAL_TIERS,
    analyze_convertible_double_low,
    check_capital_tiers,
    compute_cost_attribution,
    compute_return_attribution,
)
from finboard_backtest.convertible_double_low.backtest import (
    ConvertibleBacktestResult,
    ConvertibleTrade,
    HoldingPeriod,
)
from finboard_backtest.convertible_double_low.config import ConvertibleDoubleLowConfig


def _make_result(
    final_equity: Decimal = Decimal("110000"),
    trades: tuple[ConvertibleTrade, ...] = (),
    holdings: tuple[HoldingPeriod, ...] = (),
    equity_curve: tuple[tuple[date, Decimal], ...] = (),
    commission: Decimal = Decimal("500"),
    slippage: Decimal = Decimal("200"),
    coupon_income: Decimal = Decimal("0"),
    daily_holdings_count: tuple[tuple[date, int], ...] | None = None,
) -> ConvertibleBacktestResult:
    cfg = ConvertibleDoubleLowConfig(capital=Decimal("100000"))
    if daily_holdings_count is None:
        daily_holdings_count = tuple(
            (date(2023, 1, 1) + timedelta(days=i), 10) for i in range(50)
        )
    return ConvertibleBacktestResult(
        config=cfg,
        trades=trades,
        holdings=holdings,
        equity_curve=equity_curve,
        position_weights=(),
        daily_holdings_count=daily_holdings_count,
        rebalance_dates=(date(2023, 1, 2),),
        cost_breakdown={
            "commission": commission,
            "slippage": slippage,
            "stamp_tax": Decimal("0"),
        },
        coupon_income=coupon_income,
        redemption_proceeds=Decimal("0"),
        final_equity=final_equity,
    )


class TestCostAttribution:
    def test_basic(self) -> None:
        result = _make_result()
        attr = compute_cost_attribution(result)
        assert attr.total_cost == Decimal("700")
        assert attr.commission == Decimal("500")
        assert attr.slippage == Decimal("200")
        assert attr.stamp_tax == Decimal("0")
        assert attr.cost_pct_of_capital == Decimal("0.7")

    def test_zero_cost(self) -> None:
        result = _make_result(commission=Decimal("0"), slippage=Decimal("0"))
        attr = compute_cost_attribution(result)
        assert attr.total_cost == Decimal("0")


class TestReturnAttribution:
    def test_positive_return(self) -> None:
        d1 = date(2023, 1, 1)
        d2 = date(2023, 3, 1)
        holdings = (
            HoldingPeriod(
                code="AAA",
                entry_date=d1,
                entry_price=Decimal("100"),
                exit_date=d2,
                exit_price=Decimal("110"),
                quantity=Decimal("100"),
                pnl=Decimal("1000"),
                exit_reason="rank_exit",
            ),
        )
        result = _make_result(final_equity=Decimal("110000"), holdings=holdings)
        attr = compute_return_attribution(result)
        assert attr.total_return_pct == Decimal("10")
        assert attr.num_trades == 0
        assert attr.num_winning_trades == 1

    def test_negative_return(self) -> None:
        result = _make_result(final_equity=Decimal("90000"))
        attr = compute_return_attribution(result)
        assert attr.total_return_pct == Decimal("-10")


class TestCapitalTiers:
    def test_three_tiers(self) -> None:
        result = _make_result()
        tiers = check_capital_tiers(result)
        assert len(tiers) == 3
        assert tiers[0].capital == CAPITAL_TIERS[0]
        assert tiers[2].capital == CAPITAL_TIERS[2]

    def test_small_capital_feasibility(self) -> None:
        result = _make_result()
        tiers = check_capital_tiers(result)
        assert all(isinstance(t.is_feasible, bool) for t in tiers)


class TestAnalyze:
    def test_profitable_strategy(self) -> None:
        equity_curve = tuple(
            (date(2023, 1, 1) + timedelta(days=i), Decimal("100000") + Decimal(i * 100))
            for i in range(50)
        )
        result = _make_result(
            final_equity=Decimal("105000"),
            equity_curve=equity_curve,
        )
        analysis = analyze_convertible_double_low(result)
        assert analysis.total_return_pct == Decimal("5")
        assert not analysis.rejected

    def test_negative_return_rejected(self) -> None:
        result = _make_result(final_equity=Decimal("90000"))
        analysis = analyze_convertible_double_low(result)
        assert analysis.rejected
        assert analysis.reject_reason == "negative_return"

    def test_max_drawdown(self) -> None:
        equity_curve = (
            (date(2023, 1, 1), Decimal("110000")),
            (date(2023, 1, 2), Decimal("100000")),
            (date(2023, 1, 3), Decimal("105000")),
        )
        result = _make_result(equity_curve=equity_curve)
        analysis = analyze_convertible_double_low(result)
        assert analysis.max_drawdown_pct > Decimal("0")
