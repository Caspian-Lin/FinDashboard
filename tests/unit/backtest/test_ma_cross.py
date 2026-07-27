"""均线交叉策略单元测试 + 端到端回测集成测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock
from finboard_backtest.config import BacktestConfig
from finboard_backtest.context import BacktestContext
from finboard_backtest.engine import BacktestEngine
from finboard_backtest.result import BacktestResult
from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)
SYMBOL_ALT = Symbol(code="510500.SH", market=Market.A_SHARE)


def _make_bar(date_str: str, close: str, o: str | None = None) -> Bar:
    c = Decimal(close)
    return Bar(
        symbol=SYMBOL,
        period=BarPeriod.D1,
        timestamp=datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC),
        open=Decimal(o or close),
        high=c + Decimal("0.05"),
        low=c - Decimal("0.05"),
        close=c,
        volume=Decimal("1000000"),
    )


def _make_bar_ex(
    symbol: Symbol,
    day: int,
    close: str,
    *,
    volume: str = "1000000",
    amount: str = "0",
) -> Bar:
    c = Decimal(close)
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=c,
        high=c + Decimal("0.05"),
        low=c - Decimal("0.05"),
        close=c,
        volume=Decimal(volume),
        amount=Decimal(amount),
    )


class TestMaCrossSignal:
    """均线交叉信号检测。"""

    @pytest.mark.unit
    async def test_gold_cross_triggers_buy(self) -> None:
        strategy = MaCrossStrategy(
            strategy_id="test",
            symbol_code="510300.SH",
            short_window=2,
            long_window=4,
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )

        # 构造金叉:前4根下跌(short < long),然后short快速回升上穿
        prices = ["5.0", "4.5", "4.0", "3.5", "4.5", "5.5"]
        for i, p in enumerate(prices):
            d = f"2024-01-{i + 1:02d}"
            bar = _make_bar(d, p)
            clock.advance_to(bar.timestamp)
            broker.on_new_bar(bar)
            event = MarketDataEvent(type=MarketDataEventType.BAR, bar=bar)
            await strategy.on_market_data(event, ctx)  # type: ignore[arg-type]

            for _ev in await broker.drain_events():
                pass

        # 应有买单
        assert len(broker.fills) > 0
        assert broker.fills[0].side.value == "buy"

    @pytest.mark.unit
    async def test_no_signal_before_warmup(self) -> None:
        """长期均线未满时不下单。"""
        strategy = MaCrossStrategy(
            strategy_id="test",
            symbol_code="510300.SH",
            short_window=5,
            long_window=20,
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )

        # Only feed 10 bars (< 20 needed)
        for i in range(10):
            bar = _make_bar(f"2024-01-{i + 1:02d}", "4.00")
            clock.advance_to(bar.timestamp)
            broker.on_new_bar(bar)
            event = MarketDataEvent(type=MarketDataEventType.BAR, bar=bar)
            await strategy.on_market_data(event, ctx)  # type: ignore[arg-type]

        assert len(broker.fills) == 0


class TestEndToEndBacktest:
    """端到端回测:合成数据 → MA Cross 策略 → BacktestEngine → 绩效报告。"""

    @pytest.mark.unit
    async def test_full_backtest_with_synthetic_data(self) -> None:
        """使用合成数据验证完整回测链路。"""
        from finboard_shared.models import Symbol as Sym

        # 构造一段先跌后涨的价格序列,触发金叉和死叉
        # 30 天:前15天从5.0跌到3.5,后15天从3.5涨到5.0
        prices = (
            [5.0 - i * 0.1 for i in range(15)]  # 下跌: 5.0 → 3.6
            + [3.6 + i * 0.1 for i in range(15)]  # 上涨: 3.6 → 5.0
        )
        # 再加30天震荡
        prices += [4.5 + (1 if i % 4 < 2 else -1) * 0.2 for i in range(30)]

        start_date = date(2024, 1, 1)

        synthetic_bars = [
            Bar(
                symbol=SYMBOL,
                period=BarPeriod.D1,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC).replace(
                    day=1 + i % 28, month=1 + i // 28
                ),
                open=Decimal(str(p)),
                high=Decimal(str(p + 0.1)),
                low=Decimal(str(p - 0.1)),
                close=Decimal(str(p)),
                volume=Decimal("1000000"),
            )
            for i, p in enumerate(prices)
        ]

        class SyntheticProvider:
            """内存数据提供者,返回预设的 Bar 列表。"""

            async def fetch_bars(
                self,
                symbol: Sym,
                period: BarPeriod,
                start: date,
                end: date,
                *,
                adjust: str = "qfq",
            ) -> list[Bar]:
                return synthetic_bars

        strategy = MaCrossStrategy(
            strategy_id="ma_cross_test",
            symbol_code="510300.SH",
            short_window=5,
            long_window=10,
        )

        config = BacktestConfig(
            symbols=["510300.SH"],
            start=start_date,
            end=date(2024, 12, 31),
            initial_capital=Decimal("100000"),
        )

        engine = BacktestEngine(
            strategy=strategy,
            data_provider=SyntheticProvider(),
            config=config,
        )
        result = await engine.run()

        # 验证回测产出结构完整
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) == len(prices)
        assert result.start_date is not None
        assert result.end_date is not None
        assert result.initial_capital == Decimal("100000")
        # 至少有交易(价格先跌后涨应触发金叉)
        assert result.trade_count > 0
        # 权益应为正
        assert result.final_equity > 0
        # 应有佣金支出
        assert result.commission_paid > 0
        # 打印报告确认格式
        summary = result.summary()
        assert "回测报告" in summary
        assert "总收益率" in summary

    @pytest.mark.unit
    async def test_backtest_flat_market_no_trades(self) -> None:
        """横盘市场不触发均线交叉。"""
        from finboard_shared.models import Symbol as Sym

        # 30 天恒定价格
        synthetic_bars = [
            Bar(
                symbol=SYMBOL,
                period=BarPeriod.D1,
                timestamp=datetime(2024, 1, i + 1, tzinfo=UTC),
                open=Decimal("4.00"),
                high=Decimal("4.00"),
                low=Decimal("4.00"),
                close=Decimal("4.00"),
            )
            for i in range(30)
        ]

        class FlatProvider:
            async def fetch_bars(
                self,
                symbol: Sym,
                period: BarPeriod,
                start: date,
                end: date,
                *,
                adjust: str = "qfq",
            ) -> list[Bar]:
                return synthetic_bars

        strategy = MaCrossStrategy(
            strategy_id="ma_cross_flat",
            symbol_code="510300.SH",
            short_window=5,
            long_window=10,
        )

        config = BacktestConfig(
            symbols=["510300.SH"],
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )

        engine = BacktestEngine(
            strategy=strategy,
            data_provider=FlatProvider(),
            config=config,
        )
        result = await engine.run()

        # 横盘不应触发交易
        assert result.trade_count == 0
        # 权益不变(无费用)
        assert result.total_return == pytest.approx(0.0)


class TestMaCrossUniverse:
    """动态选股 (issue #43) —— 策略层 Bar 规则选股与退出清仓。"""

    @pytest.mark.unit
    async def test_default_all_mode_preserves_legacy_behaviour(self) -> None:
        """默认 universe_mode=all 时,选股器不参与,所有标的均参与信号生成。"""
        strategy = MaCrossStrategy(
            strategy_id="legacy",
            short_window=2,
            long_window=4,
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )
        # 直接走金叉触发序列,与未引入选股器时一致
        prices = ["5.0", "4.5", "4.0", "3.5", "4.5", "5.5"]
        for i, p in enumerate(prices):
            bar = _make_bar(f"2024-01-{i + 1:02d}", p)
            clock.advance_to(bar.timestamp)
            broker.on_new_bar(bar)
            event = MarketDataEvent(type=MarketDataEventType.BAR, bar=bar)
            await strategy.on_market_data(event, ctx)  # type: ignore[arg-type]
            for _ev in await broker.drain_events():
                pass
        assert len(broker.fills) > 0
        assert strategy._universe.config.enabled() is False

    @pytest.mark.unit
    async def test_unselected_symbol_does_not_trigger_buy(self) -> None:
        """未入选标的不参与金叉买入。

        构造:lookback=10 + 强制成交额阈值很大 → 选股器永不入选;
        即便价格序列触发金叉,策略也不下单。
        """
        strategy = MaCrossStrategy(
            strategy_id="filtered",
            short_window=2,
            long_window=4,
            universe_mode="liquidity_momentum",
            universe_lookback=4,
            universe_min_avg_amount=Decimal("1e12"),  # 永远不满足
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )
        prices = ["5.0", "4.5", "4.0", "3.5", "4.5", "5.5"]
        for i, p in enumerate(prices):
            bar = _make_bar(f"2024-01-{i + 1:02d}", p)
            clock.advance_to(bar.timestamp)
            broker.on_new_bar(bar)
            event = MarketDataEvent(type=MarketDataEventType.BAR, bar=bar)
            await strategy.on_market_data(event, ctx)  # type: ignore[arg-type]
            for _ev in await broker.drain_events():
                pass
        # 选股器永不入选,所以即便金叉也被屏蔽
        assert len(broker.fills) == 0
        assert strategy._universe.is_selected(SYMBOL.code) is False

    @pytest.mark.unit
    async def test_exit_clear_submits_sell_intent_through_context(self) -> None:
        """退出转换时通过 ctx.submit_order 提交卖出意图。

        策略记录了一笔内部持仓后,在标的退出选股池时,应通过 ctx 发出
        卖出订单(走完整 Risk → OrderManager → Broker 链路,不直接改持仓)。
        """
        strategy = MaCrossStrategy(
            strategy_id="exit-clear",
            short_window=2,
            long_window=4,
            universe_mode="liquidity_momentum",
            universe_lookback=1,
            universe_min_avg_amount=Decimal("100"),
            universe_exit_clear=True,
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )

        # 直接模拟策略已持有标的(避免金叉触发的复杂性)
        strategy._positions[SYMBOL.code] = Decimal("100")
        strategy._symbol_objs[SYMBOL.code] = SYMBOL

        # lookback=1 + amount=200 → 入选
        enter_bar = _make_bar_ex(SYMBOL, 1, "10", volume="10", amount="200")
        clock.advance_to(enter_bar.timestamp)
        broker.on_new_bar(enter_bar)
        await strategy.on_market_data(
            MarketDataEvent(type=MarketDataEventType.BAR, bar=enter_bar),
            ctx,  # type: ignore[arg-type]
        )
        for _ev in await broker.drain_events():
            pass
        assert strategy._universe.is_selected(SYMBOL.code) is True

        # 滑入低成交额 → 退出,触发清仓
        exit_bar = _make_bar_ex(SYMBOL, 2, "10", volume="10", amount="10")
        clock.advance_to(exit_bar.timestamp)
        broker.on_new_bar(exit_bar)
        await strategy.on_market_data(
            MarketDataEvent(type=MarketDataEventType.BAR, bar=exit_bar),
            ctx,  # type: ignore[arg-type]
        )
        for _ev in await broker.drain_events():
            pass

        # 应产生 1 笔卖出订单(100 股),走 broker 即时成交
        sells = [f for f in broker.fills if f.side.value == "sell"]
        assert len(sells) == 1
        assert sells[0].quantity == Decimal("100")
        # 内部持仓记账也归零
        assert strategy._positions[SYMBOL.code] == 0

    @pytest.mark.unit
    async def test_exit_clear_disabled_keeps_position(self) -> None:
        """``exit_clear=False`` 时,退出仅停止新买入,不强制平仓。"""
        strategy = MaCrossStrategy(
            strategy_id="no-exit",
            short_window=2,
            long_window=4,
            universe_mode="liquidity_momentum",
            universe_lookback=1,
            universe_min_avg_amount=Decimal("100"),
            universe_exit_clear=False,
        )
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(AccountId("test"), {})
        clock = SimulatedClock()
        ctx = BacktestContext(
            strategy_id=strategy.strategy_id,
            broker=broker,
            clock=clock,
            account_id=AccountId("test"),
        )

        strategy._positions[SYMBOL.code] = Decimal("100")
        strategy._symbol_objs[SYMBOL.code] = SYMBOL

        enter_bar = _make_bar_ex(SYMBOL, 1, "10", volume="10", amount="200")
        broker.on_new_bar(enter_bar)
        await strategy.on_market_data(
            MarketDataEvent(type=MarketDataEventType.BAR, bar=enter_bar),
            ctx,  # type: ignore[arg-type]
        )
        for _ev in await broker.drain_events():
            pass

        exit_bar = _make_bar_ex(SYMBOL, 2, "10", volume="10", amount="10")
        broker.on_new_bar(exit_bar)
        await strategy.on_market_data(
            MarketDataEvent(type=MarketDataEventType.BAR, bar=exit_bar),
            ctx,  # type: ignore[arg-type]
        )
        # 退出但没有清仓
        assert strategy._universe.is_selected(SYMBOL.code) is False
        assert strategy._positions[SYMBOL.code] == Decimal("100")
        assert len(broker.fills) == 0
