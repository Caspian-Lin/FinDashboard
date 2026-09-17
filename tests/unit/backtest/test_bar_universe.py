"""Bar 规则选股组件 (``BarUniverseSelector``) 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finboard_backtest.bar_universe import (
    BarUniverseConfig,
    BarUniverseMode,
    BarUniverseSelector,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL_A = Symbol(code="000001.SZ", market=Market.A_SHARE)
SYMBOL_B = Symbol(code="000002.SZ", market=Market.A_SHARE)


def _bar(
    symbol: Symbol,
    day: int,
    close: str,
    *,
    volume: str = "1000",
    amount: str = "0",
) -> Bar:
    return Bar(
        symbol=symbol,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(volume),
        amount=Decimal(amount),
    )


@pytest.mark.unit
def test_all_mode_keeps_legacy_behaviour() -> None:
    """``ALL`` 模式默认让所有候选标的入选,与既有策略行为完全一致。"""
    selector = BarUniverseSelector(BarUniverseConfig())
    assert selector.config.mode is BarUniverseMode.ALL
    assert selector.config.enabled() is False

    # 第 1 根 Bar 即入选,无需预热
    bar = _bar(SYMBOL_A, day=1, close="10", amount="10000")
    assert selector.update(bar) is True
    assert selector.is_selected(SYMBOL_A.code) is True
    # transitioned_out 在 ALL 模式下永不触发
    assert selector.transitioned_out(SYMBOL_A.code) is False


@pytest.mark.unit
def test_lookback_warmup_blocks_selection() -> None:
    """``LIQUIDITY_MOMENTUM`` 在窗口填满前禁止入选,防止读取未来数据。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=3,
            min_avg_amount=Decimal("1"),
        )
    )

    # 只填 2 根 (< 3),不入选
    assert selector.update(_bar(SYMBOL_A, 1, "10", amount="100")) is False
    assert selector.update(_bar(SYMBOL_A, 2, "11", amount="100")) is False
    assert selector.is_selected(SYMBOL_A.code) is False

    # 第 3 根:窗口填满 + 满足阈值 → 入选
    assert selector.update(_bar(SYMBOL_A, 3, "12", amount="100")) is True
    assert selector.is_selected(SYMBOL_A.code) is True


@pytest.mark.unit
def test_min_avg_amount_threshold() -> None:
    """平均成交额 < 阈值时不入选,>= 阈值时入选。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_avg_amount=Decimal("200"),
        )
    )
    # 平均 150 < 200 → 不入选
    assert selector.update(_bar(SYMBOL_A, 1, "10", amount="100")) is False
    assert selector.update(_bar(SYMBOL_A, 2, "10", amount="200")) is False

    # 第 3 根滑入 250,窗口 (200, 250) 平均 225 >= 200 → 入选
    assert selector.update(_bar(SYMBOL_A, 3, "10", amount="250")) is True


@pytest.mark.unit
def test_amount_fallback_uses_close_times_volume() -> None:
    """成交额缺失/为 0 时回退为 ``close * volume``。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_avg_amount=Decimal("500"),
        )
    )
    # 不填 amount(默认 "0"),回退 = 10 * 100 = 1000 → 平均 1000 >= 500 入选
    bar1 = _bar(SYMBOL_A, 1, "10", volume="100", amount="0")
    bar2 = _bar(SYMBOL_A, 2, "10", volume="100", amount="0")
    assert selector.update(bar1) is False  # 预热
    assert selector.update(bar2) is True
    assert selector.is_selected(SYMBOL_A.code) is True


@pytest.mark.unit
def test_min_momentum_threshold() -> None:
    """区间动量 = close[-1] / close[0] - 1,小于阈值不入选。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_momentum=Decimal("0.20"),
        )
    )
    # 10 → 11 = +10% < 20% → 不入选
    selector.update(_bar(SYMBOL_A, 1, "10", amount="1"))
    assert selector.update(_bar(SYMBOL_A, 2, "11", amount="1")) is False

    # 滑入 13:窗口 11→13 = +18.18% < 20% → 仍不入选
    assert selector.update(_bar(SYMBOL_A, 3, "13", amount="1")) is False

    # 滑入 14:窗口 12→14? 不,deque 滑动后是 (11, 13) -> 13/11-1=0.18 还是 <0.2
    # 改为更明确:14/11 - 1 ≈ 0.272
    # deque 现在保存 [11, 13] (maxlen=2),新一根 14 推入变 [13, 14]
    # 动量 = 14/13 - 1 ≈ 0.077 < 0.2 → 不入选
    assert selector.update(_bar(SYMBOL_A, 4, "14", amount="1")) is False

    # 大涨到 16:deque [14, 16],动量 16/14 - 1 ≈ 0.143 < 0.2 → 不入选
    assert selector.update(_bar(SYMBOL_A, 5, "16", amount="1")) is False

    # 用足够大的窗口直接验证入选边界
    selector.reset()
    selector.update(_bar(SYMBOL_A, 1, "10", amount="1"))
    assert selector.update(_bar(SYMBOL_A, 2, "13", amount="1")) is True  # 30%


@pytest.mark.unit
def test_selection_exit_transition_marks_just_exited() -> None:
    """入选→退出转换在 ``transitioned_out`` 中标记,下一次 update 自动清除。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_avg_amount=Decimal("100"),
        )
    )
    # 入选:平均 200
    selector.update(_bar(SYMBOL_A, 1, "10", amount="200"))
    assert selector.update(_bar(SYMBOL_A, 2, "10", amount="200")) is True
    assert selector.transitioned_out(SYMBOL_A.code) is False

    # 滑入 50:窗口 (200, 50) 平均 125 仍 >= 100 → 仍入选
    assert selector.update(_bar(SYMBOL_A, 3, "10", amount="50")) is True

    # 再滑入 10:窗口 (50, 10) 平均 30 < 100 → 退出
    assert selector.update(_bar(SYMBOL_A, 4, "10", amount="10")) is False
    assert selector.transitioned_out(SYMBOL_A.code) is True

    # 再次 update 同一标的会清除标记
    selector.update(_bar(SYMBOL_A, 5, "10", amount="10"))
    assert selector.transitioned_out(SYMBOL_A.code) is False


@pytest.mark.unit
def test_symbol_states_are_isolated() -> None:
    """两个标的的入选状态互不影响。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_avg_amount=Decimal("100"),
        )
    )
    # A 满足阈值入选,B 不满足
    selector.update(_bar(SYMBOL_A, 1, "10", amount="200"))
    selector.update(_bar(SYMBOL_B, 1, "10", amount="10"))
    selector.update(_bar(SYMBOL_A, 2, "10", amount="200"))
    selector.update(_bar(SYMBOL_B, 2, "10", amount="10"))

    assert selector.is_selected(SYMBOL_A.code) is True
    assert selector.is_selected(SYMBOL_B.code) is False
    assert selector.transitioned_out(SYMBOL_B.code) is False


@pytest.mark.unit
def test_invalid_bars_safely_skip_selection() -> None:
    """非正价格、负成交量/成交额的异常 Bar 安全不入选。"""
    selector = BarUniverseSelector(
        BarUniverseConfig(
            mode=BarUniverseMode.LIQUIDITY_MOMENTUM,
            lookback=2,
            min_avg_amount=Decimal("1"),
        )
    )
    # close=0 → 安全不入选
    assert selector.update(_bar(SYMBOL_A, 1, "0", amount="1000")) is False
    # volume<0 → 安全不入选
    neg_vol = _bar(SYMBOL_A, 2, "10", volume="-5", amount="1000")
    assert selector.update(neg_vol) is False
    # amount<0 → 安全不入选
    neg_amt = _bar(SYMBOL_A, 3, "10", volume="100", amount="-1000")
    assert selector.update(neg_amt) is False
    assert selector.is_selected(SYMBOL_A.code) is False


@pytest.mark.unit
def test_config_validates_bounds() -> None:
    with pytest.raises(ValueError, match="lookback"):
        BarUniverseConfig(lookback=0)

    with pytest.raises(ValueError, match="min_avg_amount"):
        BarUniverseConfig(min_avg_amount=Decimal("-1"))

    with pytest.raises(ValueError, match="min_momentum"):
        BarUniverseConfig(min_momentum=Decimal("-2"))
