"""回测引擎基准选择与基准收益计算(issue #184)。

覆盖:显式基准标的单独拉取(不在回测 universe)、universe 内复用、基准缺失
返回 null + warning、benchmark_config 如实归档。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.config import BacktestConfig, BenchmarkConfig
from finboard_backtest.engine import BacktestEngine
from finboard_backtest.result import BacktestResult
from finboard_core import Strategy
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

UNIVERSE = "600519.SH"
BENCHMARK = "000300.SH"


class NoopStrategy(Strategy):
    """只记录行情、从不交易的策略,隔离基准计算。"""

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("noop")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx


def _bar(symbol: Symbol, day: int, close: Decimal) -> Bar:
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1000000"),
        amount=Decimal("0"),
        source="test",
    )


class MemoryProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.fetch_log: list[str] = []

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del period, start, end, adjust
        self.fetch_log.append(symbol.code)
        return self.bars_by_symbol.get(symbol.code, [])


def _flat_bars(symbol: Symbol) -> list[Bar]:
    """3 根平盘(收盘恒 10 元)→ 自身收益 0。"""
    return [_bar(symbol, day, Decimal("10")) for day in (2, 3, 4)]


def _rising_bars(symbol: Symbol) -> list[Bar]:
    """3 根上涨(100 → 121)→ 买入持有收益 +21%。"""
    return [_bar(symbol, day, Decimal(str(close))) for day, close in zip((2, 3, 4), (100, 110, 121), strict=True)]


@pytest.mark.unit
async def test_explicit_benchmark_fetched_outside_universe() -> None:
    """基准不在回测 universe:引擎单独拉取,基准收益来自基准行情。"""
    provider = MemoryProvider(
        {
            UNIVERSE: _flat_bars(Symbol(UNIVERSE, Market.A_SHARE)),
            BENCHMARK: _rising_bars(Symbol(BENCHMARK, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark=BenchmarkConfig(symbol=BENCHMARK),
        ),
    )
    result = await engine.run()
    assert isinstance(result, BacktestResult)
    # 基准被单独拉取(即使不在 symbols 里)
    assert BENCHMARK in provider.fetch_log
    assert len(result.benchmark_curve) == 3
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-9)
    # 策略自身无交易,组合收益 0;超额 = 0 - 0.21
    assert result.total_return == pytest.approx(0.0, abs=1e-9)
    assert result.excess_return == pytest.approx(-0.21, abs=1e-9)
    # benchmark_config 如实归档显式基准
    assert result.benchmark_config == BenchmarkConfig(symbol=BENCHMARK).as_dict()


@pytest.mark.unit
async def test_explicit_benchmark_reuses_universe_bars() -> None:
    """基准在回测 universe 内:复用已加载 bars,不重复拉取。"""
    provider = MemoryProvider(
        {
            UNIVERSE: _rising_bars(Symbol(UNIVERSE, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark=BenchmarkConfig(symbol=UNIVERSE),
        ),
    )
    result = await engine.run()
    assert provider.fetch_log.count(UNIVERSE) == 1
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-9)


@pytest.mark.unit
async def test_explicit_benchmark_beats_equal_weight_fallback() -> None:
    """显式基准优先于等权候选池兜底。"""
    other = "000002.SZ"
    provider = MemoryProvider(
        {
            UNIVERSE: _flat_bars(Symbol(UNIVERSE, Market.A_SHARE)),
            other: _flat_bars(Symbol(other, Market.A_SHARE)),
            BENCHMARK: _rising_bars(Symbol(BENCHMARK, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE, other],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark=BenchmarkConfig(symbol=BENCHMARK),
        ),
    )
    result = await engine.run()
    assert BENCHMARK in provider.fetch_log
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-9)


@pytest.mark.unit
async def test_benchmark_missing_returns_null_with_warning() -> None:
    """显式基准拉取失败:benchmark_return/excess_return 为 None,发出具名 warning。"""
    from unittest.mock import patch

    from finboard_backtest import engine as engine_module

    provider = MemoryProvider(
        {
            UNIVERSE: _flat_bars(Symbol(UNIVERSE, Market.A_SHARE)),
            BENCHMARK: [],
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark=BenchmarkConfig(symbol=BENCHMARK),
        ),
    )
    with patch.object(engine_module.logger, "warning") as warn:
        result = await engine.run()
    assert result.benchmark_return is None
    assert result.excess_return is None
    # structlog 事件名是首个位置参数;渲染目标随环境(CI/本地)不同,
    # 直接断言事件本身,不依赖 stdout/stderr。
    assert any(
        call.args and call.args[0] == "backtest.benchmark_missing"
        for call in warn.call_args_list
    )


@pytest.mark.unit
async def test_first_symbol_fallback_remains_real_benchmark() -> None:
    """无显式基准 + 等权关闭:首个标的买入持有仍为真实基准(非 null)。"""
    provider = MemoryProvider(
        {
            UNIVERSE: _rising_bars(Symbol(UNIVERSE, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark=BenchmarkConfig(symbol=None, equal_weight_universe=False),
        ),
    )
    result = await engine.run()
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-9)
    assert result.excess_return == pytest.approx(-0.21, abs=1e-9)
