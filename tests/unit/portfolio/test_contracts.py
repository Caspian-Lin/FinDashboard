"""portfolio/contracts.py 的单元测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.portfolio.contracts import (
    CAPITAL_TIERS,
    PORTFOLIO_CONTRACT_VERSION,
    AssetLotInfo,
    PortfolioConstraints,
    RebalancePlan,
    RebalanceTrade,
    Signal,
    Sleeve,
    TargetWeight,
)


class TestSignal:
    def test_valid_signal(self) -> None:
        sig = Signal(
            symbol="600519.SH",
            score=0.8,
            timestamp=date(2024, 6, 28),
            strategy_id="ma_cross",
        )
        assert sig.symbol == "600519.SH"
        assert sig.score == 0.8
        assert sig.confidence == 1.0

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            Signal(symbol="", score=1.0, timestamp=date(2024, 1, 1), strategy_id="s")

    def test_empty_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="strategy_id"):
            Signal(symbol="A", score=1.0, timestamp=date(2024, 1, 1), strategy_id="")

    def test_confidence_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Signal(
                symbol="A", score=0.5, timestamp=date(2024, 1, 1),
                strategy_id="s", confidence=1.5,
            )

    def test_confidence_negative(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Signal(
                symbol="A", score=0.5, timestamp=date(2024, 1, 1),
                strategy_id="s", confidence=-0.1,
            )


class TestPortfolioConstraints:
    def test_defaults(self) -> None:
        c = PortfolioConstraints()
        assert c.max_weight_per_asset == 0.25
        assert c.max_weight_per_sleeve == 0.40
        assert c.min_cash_buffer == 0.05
        assert c.max_leverage == 1.0
        assert c.target_volatility is None
        assert c.max_volatility is None
        assert c.rebalance_threshold == 0.05

    def test_max_investable_weight(self) -> None:
        c = PortfolioConstraints(max_leverage=1.0, min_cash_buffer=0.10)
        assert c.max_investable_weight == pytest.approx(0.9)

    def test_leverage_2x(self) -> None:
        c = PortfolioConstraints(max_leverage=2.0, min_cash_buffer=0.0)
        assert c.max_investable_weight == pytest.approx(2.0)

    def test_asset_exceeds_sleeve_raises(self) -> None:
        with pytest.raises(ValueError, match="max_weight_per_asset"):
            PortfolioConstraints(max_weight_per_asset=0.50, max_weight_per_sleeve=0.30)

    def test_leverage_below_1_raises(self) -> None:
        with pytest.raises(ValueError, match="max_leverage"):
            PortfolioConstraints(max_leverage=0.5)

    def test_target_vol_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="target_volatility"):
            PortfolioConstraints(target_volatility=-0.1)

    def test_rebalance_threshold_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="rebalance_threshold"):
            PortfolioConstraints(rebalance_threshold=0.6)


class TestTargetWeight:
    def test_valid_weights(self) -> None:
        tw = TargetWeight(
            weights={"A": 0.5, "B": 0.3},
            as_of=date(2024, 6, 28),
            strategy_id="s",
            cash_buffer=0.2,
        )
        assert tw.gross_weight == pytest.approx(0.8)
        assert tw.max_leverage == pytest.approx(1.0)
        assert tw.n_assets == 2

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            TargetWeight(
                weights={"A": -0.1}, as_of=date(2024, 1, 1), strategy_id="s",
            )

    def test_weight_of_missing(self) -> None:
        tw = TargetWeight(
            weights={"A": 0.5}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        assert tw.weight_of("B") == 0.0
        assert tw.weight_of("A") == pytest.approx(0.5)

    def test_zero_weight_not_counted(self) -> None:
        tw = TargetWeight(
            weights={"A": 0.5, "B": 0.0},
            as_of=date(2024, 1, 1), strategy_id="s", cash_buffer=0.5,
        )
        assert tw.n_assets == 1

    def test_contract_version(self) -> None:
        tw = TargetWeight(
            weights={}, as_of=date(2024, 1, 1), strategy_id="s",
        )
        assert tw.contract_version == PORTFOLIO_CONTRACT_VERSION


class TestAssetLotInfo:
    def test_stock_lot(self) -> None:
        info = AssetLotInfo(code="600519.SH", lot_size=100, multiplier=1)
        assert info.unit_factor == pytest.approx(100.0)

    def test_futures_lot(self) -> None:
        info = AssetLotInfo(code="IF2406.CFFEX", lot_size=1, multiplier=200)
        assert info.unit_factor == pytest.approx(200.0)

    def test_zero_lot_size_raises(self) -> None:
        with pytest.raises(ValueError, match="lot_size"):
            AssetLotInfo(code="A", lot_size=0)


class TestCapitalTiers:
    def test_100k(self) -> None:
        tier = CAPITAL_TIERS["100k"]
        assert tier.total_capital == pytest.approx(100_000.0)

    def test_500k(self) -> None:
        tier = CAPITAL_TIERS["500k"]
        assert tier.total_capital == pytest.approx(500_000.0)


class TestSleeve:
    def test_valid(self) -> None:
        s = Sleeve(name="equity", max_weight=0.60)
        assert s.name == "equity"

    def test_zero_max_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="max_weight"):
            Sleeve(name="bond", max_weight=0.0)


class TestRebalanceTrade:
    def test_buy_trade(self) -> None:
        t = RebalanceTrade(
            symbol="A", delta_shares=100, target_shares=200,
            current_shares=100, target_value=2000.0, current_value=1000.0,
            delta_value=1000.0,
        )
        assert t.is_buy
        assert not t.is_sell
        assert not t.is_noop

    def test_sell_trade(self) -> None:
        t = RebalanceTrade(
            symbol="A", delta_shares=-50, target_shares=50,
            current_shares=100, target_value=500.0, current_value=1000.0,
            delta_value=-500.0,
        )
        assert t.is_sell

    def test_noop_trade(self) -> None:
        t = RebalanceTrade(
            symbol="A", delta_shares=0, target_shares=100,
            current_shares=100, target_value=1000.0, current_value=1000.0,
            delta_value=0.0,
        )
        assert t.is_noop

    def test_negative_target_raises(self) -> None:
        with pytest.raises(ValueError, match="target_shares"):
            RebalanceTrade(
                symbol="A", delta_shares=0, target_shares=-1,
                current_shares=0, target_value=0, current_value=0, delta_value=0,
            )


class TestRebalancePlan:
    def test_valid_plan(self) -> None:
        plan = RebalancePlan(
            trades=[],
            total_capital=100_000.0,
            cash_before=100_000.0,
            cash_after=50_000.0,
            est_commission=5.0,
            est_tax=0.0,
            total_turnover=0.0,
            as_of=date(2024, 1, 1),
            strategy_id="s",
        )
        assert plan.n_trades == 0
        assert plan.n_active_trades == 0

    def test_negative_cash_raises(self) -> None:
        with pytest.raises(ValueError, match="cash_after"):
            RebalancePlan(
                trades=[], total_capital=100_000.0,
                cash_before=100_000.0, cash_after=-1.0,
                est_commission=0, est_tax=0, total_turnover=0,
                as_of=date(2024, 1, 1), strategy_id="s",
            )
