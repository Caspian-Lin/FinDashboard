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
from finboard_data import FactorSelectionConfig
from finboard_data.factors import FactorSnapshot
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


# ---- issue #254:选股启用时基准回退优先每期选股池 ----


class _PoolSelector:
    """恒发布且恒选同一标的的确定性 selector。"""

    def __init__(self, selected: tuple[str, ...]) -> None:
        self._selected = selected

    async def select(self, **kwargs: object) -> FactorSnapshot:
        from finboard_data import FactorSnapshot, FactorSnapshotStatus

        business_date = kwargs["business_date"]
        decision_at = kwargs["decision_at"]
        effective_date = kwargs["effective_date"]
        assert isinstance(business_date, date)
        assert isinstance(decision_at, datetime)
        assert isinstance(effective_date, date)
        return FactorSnapshot(
            decision_at=decision_at,
            business_date=business_date,
            effective_date=effective_date,
            source="test",
            dataset_versions={"daily_metrics": f"daily-{business_date}"},
            factor_version="v1",
            static_universe=self._selected,
            selected_symbols=self._selected,
            values=(),
            status=FactorSnapshotStatus.PUBLISHED,
            skip_reason=None,
            config={"enabled": True},
            checksum=f"{business_date:%Y%m%d}".ljust(64, "0"),
        )


class _SkippedSelector:
    """恒 SKIPPED 的 selector(模拟 research_db 缺 profiles 整期空转)。"""

    async def select(self, **kwargs: object) -> FactorSnapshot:
        from finboard_data import FactorSnapshot, FactorSnapshotStatus

        business_date = kwargs["business_date"]
        decision_at = kwargs["decision_at"]
        effective_date = kwargs["effective_date"]
        assert isinstance(business_date, date)
        assert isinstance(decision_at, datetime)
        assert isinstance(effective_date, date)
        return FactorSnapshot(
            decision_at=decision_at,
            business_date=business_date,
            effective_date=effective_date,
            source="test",
            dataset_versions={},
            factor_version="v1",
            static_universe=(UNIVERSE,),
            selected_symbols=(),
            values=(),
            status=FactorSnapshotStatus.SKIPPED,
            skip_reason="profile_missing",
            config={"enabled": True},
            checksum=f"{business_date:%Y%m%d}".ljust(64, "0"),
        )


@pytest.mark.unit
async def test_selection_pool_benchmark_preferred_over_static_pool() -> None:
    """选股启用:回退基准跟随每期选股结果(动态等权),不用静态全池。"""
    other = "000002.SZ"
    provider = MemoryProvider(
        {
            UNIVERSE: _rising_bars(Symbol(UNIVERSE, Market.A_SHARE)),
            other: _flat_bars(Symbol(other, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE, other],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            selection=FactorSelectionConfig(enabled=True),
        ),
        factor_selector=_PoolSelector((UNIVERSE,)),  # type: ignore[arg-type]
    )
    result = await engine.run()
    # 选股池只含 UNIVERSE(100→121):动态等权 = +21%;
    # 静态全池等权买入持有只有 +10.5%——旧口径即由此失真。
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-6)
    assert result.benchmark_source == "equal_weight_selection_pool"
    assert "基准来源:   equal_weight_selection_pool" in result.summary()


@pytest.mark.unit
async def test_selection_all_skipped_falls_back_to_static_with_source() -> None:
    """选股启用但整期 SKIPPED:选股池不可用,回退静态口径且来源可见。"""
    other = "000002.SZ"
    provider = MemoryProvider(
        {
            UNIVERSE: _flat_bars(Symbol(UNIVERSE, Market.A_SHARE)),
            other: _flat_bars(Symbol(other, Market.A_SHARE)),
        }
    )
    engine = BacktestEngine(
        strategy=NoopStrategy(),
        data_provider=provider,
        config=BacktestConfig(
            symbols=[UNIVERSE, other],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            selection=FactorSelectionConfig(enabled=True),
        ),
        factor_selector=_SkippedSelector(),  # type: ignore[arg-type]
    )
    result = await engine.run()
    assert result.benchmark_return is not None
    assert result.benchmark_source == "equal_weight_static_pool"


@pytest.mark.unit
async def test_explicit_benchmark_still_beats_selection_pool() -> None:
    """显式基准优先级最高,选股启用也不改变。"""
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
            selection=FactorSelectionConfig(enabled=True),
            benchmark=BenchmarkConfig(symbol=BENCHMARK),
        ),
        factor_selector=_PoolSelector((UNIVERSE,)),  # type: ignore[arg-type]
    )
    result = await engine.run()
    assert result.benchmark_return == pytest.approx(0.21, abs=1e-9)
    assert result.benchmark_source == f"explicit_symbol:{BENCHMARK}"
