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
