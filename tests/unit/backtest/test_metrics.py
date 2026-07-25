"""绩效指标计算测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finboard_backtest.metrics import (
    buy_and_hold_return,
    max_drawdown,
    sharpe_ratio,
    total_return,
    win_rate,
)


class TestTotalReturn:
    @pytest.mark.unit
    def test_positive_return(self) -> None:
        curve = [
            (date(2024, 1, 1), Decimal("100000")),
            (date(2024, 6, 30), Decimal("110000")),
        ]
        assert total_return(curve) == pytest.approx(0.10)

    @pytest.mark.unit
    def test_negative_return(self) -> None:
        curve = [
            (date(2024, 1, 1), Decimal("100000")),
            (date(2024, 6, 30), Decimal("95000")),
        ]
        assert total_return(curve) == pytest.approx(-0.05)

    @pytest.mark.unit
    def test_empty_curve(self) -> None:
        assert total_return([]) == 0.0


class TestMaxDrawdown:
    @pytest.mark.unit
    def test_simple_drawdown(self) -> None:
        curve = [
            (date(2024, 1, 1), Decimal("100")),
            (date(2024, 1, 2), Decimal("120")),  # peak
            (date(2024, 1, 3), Decimal("90")),   # 25% drawdown
            (date(2024, 1, 4), Decimal("95")),
        ]
        dd = max_drawdown(curve)
        assert dd == pytest.approx(-0.25)

    @pytest.mark.unit
    def test_no_drawdown(self) -> None:
        curve = [
            (date(2024, 1, 1), Decimal("100")),
            (date(2024, 1, 2), Decimal("110")),
            (date(2024, 1, 3), Decimal("120")),
        ]
        assert max_drawdown(curve) == pytest.approx(0.0)


class TestSharpeRatio:
    @pytest.mark.unit
    def test_positive_sharpe(self) -> None:
        # Steadily increasing equity → positive sharpe
        curve = [
            (date(2024, 1, 1), Decimal("100")),
            (date(2024, 1, 2), Decimal("101")),
            (date(2024, 1, 3), Decimal("102")),
            (date(2024, 1, 4), Decimal("103")),
            (date(2024, 1, 5), Decimal("104")),
        ]
        sr = sharpe_ratio(curve)
        assert sr > 0

    @pytest.mark.unit
    def test_volatile_returns(self) -> None:
        # Alternating up/down → lower sharpe
        curve = [
            (date(2024, 1, 1), Decimal("100")),
            (date(2024, 1, 2), Decimal("110")),
            (date(2024, 1, 3), Decimal("100")),
            (date(2024, 1, 4), Decimal("110")),
            (date(2024, 1, 5), Decimal("100")),
        ]
        sr = sharpe_ratio(curve)
        assert sr < 5  # Should be modest due to volatility


class TestWinRate:
    @pytest.mark.unit
    def test_all_wins(self) -> None:
        from finboard_shared.identifiers import ClientOrderId
        from finboard_shared.models import Fill
        from finboard_shared.types import Side

        fills = [
            Fill(
                fill_id="1",
                client_order_id=ClientOrderId("a"),
                symbol=None,  # type: ignore[arg-type]
                side=Side.BUY,
                quantity=Decimal("100"),
                price=Decimal("4.00"),
            ),
            Fill(
                fill_id="2",
                client_order_id=ClientOrderId("b"),
                symbol=None,  # type: ignore[arg-type]
                side=Side.SELL,
                quantity=Decimal("100"),
                price=Decimal("5.00"),
            ),
        ]
        assert win_rate(fills) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_all_losses(self) -> None:
        from finboard_shared.identifiers import ClientOrderId
        from finboard_shared.models import Fill
        from finboard_shared.types import Side

        fills = [
            Fill(
                fill_id="1",
                client_order_id=ClientOrderId("a"),
                symbol=None,  # type: ignore[arg-type]
                side=Side.BUY,
                quantity=Decimal("100"),
                price=Decimal("5.00"),
            ),
            Fill(
                fill_id="2",
                client_order_id=ClientOrderId("b"),
                symbol=None,  # type: ignore[arg-type]
                side=Side.SELL,
                quantity=Decimal("100"),
                price=Decimal("4.00"),
            ),
        ]
        assert win_rate(fills) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_no_sells(self) -> None:
        from finboard_shared.identifiers import ClientOrderId
        from finboard_shared.models import Fill
        from finboard_shared.types import Side

        fills = [
            Fill(
                fill_id="1",
                client_order_id=ClientOrderId("a"),
                symbol=None,  # type: ignore[arg-type]
                side=Side.BUY,
                quantity=Decimal("100"),
                price=Decimal("4.00"),
            ),
        ]
        assert win_rate(fills) == 0.0


class TestBuyAndHold:
    @pytest.mark.unit
    def test_basic(self) -> None:
        bars = [
            (date(2024, 1, 1), Decimal("4.00")),
            (date(2024, 1, 2), Decimal("5.00")),
        ]
        curve = buy_and_hold_return(bars, Decimal("10000"))
        # 10000 / 4.00 = 2500 shares
        assert curve[0][1] == Decimal("10000")
        assert curve[1][1] == Decimal("12500")
