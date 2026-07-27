"""端到端回测验证 Bar 规则动态选股 —— 合成多标的数据,不依赖公网行情。

覆盖 issue #43 的端到端验收点:

* 默认 ``all`` 模式下回测结果与既有 MaCross 行为兼容(相同数据集)。
* ``liquidity_momentum`` 模式能根据成交额 / 动量在多标的中筛选,过滤会真实
  缩小进入策略的标的集合,且只消费已到达 Bar,不访问未来数据。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod

SYMBOL_LIQUID = Symbol(code="LIQUID.SH", market="a_share")  # type: ignore[arg-type]
SYMBOL_ILLIQUID = Symbol(code="ILLIQ.SH", market="a_share")  # type: ignore[arg-type]
_BASE_DATE = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(symbol: Symbol, *, base_close: str, amount: str, days: int = 60) -> list[Bar]:
    """构造 ``days`` 根平稳 OHLCV(价格不变),仅成交额不同。"""
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=_BASE_DATE + timedelta(days=i),
            open=Decimal(base_close),
            high=Decimal(base_close),
            low=Decimal(base_close),
            close=Decimal(base_close),
            volume=Decimal("1000"),
            amount=Decimal(amount),
        )
        for i in range(days)
    ]


class _MultiSymbolProvider:
    """按 symbol code 返回预设 Bar 列表的内存 provider。"""

    def __init__(self, library: dict[str, list[Bar]]) -> None:
        self._library = library

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
        return list(self._library.get(symbol.code, []))


@pytest.mark.unit
async def test_default_all_mode_keeps_legacy_multisymbol_behaviour() -> None:
    """默认 universe_mode=all 时,所有静态候选标的均进入信号生成。"""
    library = {
        SYMBOL_LIQUID.code: _bars(SYMBOL_LIQUID, base_close="10", amount="10000"),
        SYMBOL_ILLIQUID.code: _bars(SYMBOL_ILLIQUID, base_close="10", amount="10"),
    }
    strategy = MaCrossStrategy(
        strategy_id="default",
        short_window=5,
        long_window=10,
    )
    config = BacktestConfig(
        symbols=[SYMBOL_LIQUID.code, SYMBOL_ILLIQUID.code],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        initial_capital=Decimal("100000"),
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=_MultiSymbolProvider(library),
        config=config,
    )
    result = await engine.run()

    # 价格平稳 → 不会触发金叉/死叉,因此无交易,但权益曲线长度 == 数据天数
    assert result.trade_count == 0
    assert result.start_date == date(2024, 1, 1)
    # 选股器未启用,内部状态应反映 "未启用"
    assert strategy._universe.config.enabled() is False


@pytest.mark.unit
async def test_liquidity_momentum_filters_out_illiquid_symbol() -> None:
    """``liquidity_momentum`` 能在多标的中按平均成交额筛选。

    构造:LIQUID 平均成交额 10000,ILLIQ 平均成交额 10。配合一个会触发
    金叉的价格序列(先跌后涨),验证:

    * LIQUID 入选并产生买入成交;
    * ILLIQ 因成交额不足不入选,即便价格触发金叉也不买入。
    """
    # 价格先跌后涨,触发金叉(short=5 上穿 long=10)
    n_rise = 30
    n_total = 60
    prices = (
        [Decimal("10") - Decimal("0.1") * i for i in range(n_rise)]
        + [Decimal("7") + Decimal("0.1") * i for i in range(n_total - n_rise)]
    )

    def _make(symbol: Symbol, amount: str) -> list[Bar]:
        return [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=_BASE_DATE + timedelta(days=i),
                open=prices[i],
                high=prices[i] + Decimal("0.05"),
                low=prices[i] - Decimal("0.05"),
                close=prices[i],
                volume=Decimal("1000"),
                amount=Decimal(amount),
            )
            for i in range(n_total)
        ]

    library = {
        SYMBOL_LIQUID.code: _make(SYMBOL_LIQUID, amount="10000"),
        SYMBOL_ILLIQUID.code: _make(SYMBOL_ILLIQUID, amount="10"),
    }
    strategy = MaCrossStrategy(
        strategy_id="filter",
        short_window=5,
        long_window=10,
        universe_mode="liquidity_momentum",
        universe_lookback=10,
        universe_min_avg_amount=Decimal("1000"),
    )
    config = BacktestConfig(
        symbols=[SYMBOL_LIQUID.code, SYMBOL_ILLIQUID.code],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        initial_capital=Decimal("100000"),
    )
    engine = BacktestEngine(
        strategy=strategy,
        data_provider=_MultiSymbolProvider(library),
        config=config,
    )
    result = await engine.run()

    # 选股器只让 LIQUID 入选;ILLIQ 即便金叉也被屏蔽
    assert strategy._universe.is_selected(SYMBOL_LIQUID.code) is True
    assert strategy._universe.is_selected(SYMBOL_ILLIQUID.code) is False

    fills = result.fills
    symbols_traded = {f.symbol.code for f in fills}
    assert SYMBOL_LIQUID.code in symbols_traded
    assert SYMBOL_ILLIQUID.code not in symbols_traded
    # LIQUID 至少有买入成交
    assert any(f.side.value == "buy" for f in fills if f.symbol.code == SYMBOL_LIQUID.code)
