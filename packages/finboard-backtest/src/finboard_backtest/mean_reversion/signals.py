"""短周期均值回归信号计算。

支持四种预注册信号族:

* **Z_SCORE** — 滚动 z-score:z = (close - SMA(n)) / std(n)。
  z < -entry → 入场(超卖);z > -exit → 出场。
* **BOLLINGER** — Bollinger %B:%B = (close - lower) / (upper - lower)。
  %B < entry_threshold → 入场;%B > exit_threshold → 出场。
* **RSI** — RSI(period) 震荡指标。
  RSI < entry_threshold → 入场(超卖);RSI > exit_threshold → 出场。
* **REVERSAL** — N 日收益率。
  ret < -entry_threshold → 入场(急跌);ret > -exit_threshold → 出场。

所有函数均为纯函数,相同输入永远产出相同输出。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    SignalFamily,
)


@dataclass(frozen=True, slots=True)
class SignalResult:
    """单标的单 Bar 的信号计算结果。"""

    symbol: str
    value: float
    """信号原始值(z-score / %B / RSI / N-day return)。"""
    should_enter: bool
    """是否触发入场信号。"""
    should_exit: bool
    """是否触发出场信号。"""
    family: str


@dataclass(frozen=True, slots=True)
class SignalDirection:
    """入场/出场方向提示。"""

    enter: bool
    exit: bool


def _sma(values: Sequence[float], window: int) -> float:
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def _std(values: Sequence[float], window: int) -> float:
    """总体标准差(除以 N)。"""
    if len(values) < window:
        return float("nan")
    slice_ = values[-window:]
    mean = sum(slice_) / window
    var = sum((x - mean) ** 2 for x in slice_) / window
    return math.sqrt(var)


def compute_zscore(
    closes: Sequence[float],
    window: int,
) -> float:
    """滚动 z-score = (close - SMA(window)) / std(window)。"""
    if len(closes) < window:
        return float("nan")
    mean = _sma(closes, window)
    sd = _std(closes, window)
    if sd == 0 or sd != sd:  # NaN check
        return float("nan")
    return (closes[-1] - mean) / sd


def compute_bollinger_position(
    closes: Sequence[float],
    window: int,
    num_std: float,
) -> float:
    """Bollinger %B = (close - lower) / (upper - lower)。

    %B < 0 → 价格在下轨之下;%B > 1 → 价格在上轨之上。
    """
    if len(closes) < window:
        return float("nan")
    mean = _sma(closes, window)
    sd = _std(closes, window)
    if sd == 0 or sd != sd:
        return float("nan")
    upper = mean + num_std * sd
    lower = mean - num_std * sd
    band_width = upper - lower
    if band_width == 0:
        return float("nan")
    return (closes[-1] - lower) / band_width


def compute_rsi(closes: Sequence[float], period: int) -> float:
    """RSI(period) — Wilder 平滑法。

    返回 0-100 之间的值;数据不足返回 NaN。
    """
    if len(closes) < period + 1:
        return float("nan")

    gains: list[float] = []
    losses: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_reversal(
    closes: Sequence[float],
    lookback: int,
) -> float:
    """N 日收益率 = close[-1] / close[-lookback-1] - 1。"""
    if len(closes) < lookback + 1:
        return float("nan")
    start = closes[-(lookback + 1)]
    end = closes[-1]
    if start <= 0:
        return float("nan")
    return (end / start) - 1.0


def _compute_signal_value(
    closes: Sequence[float],
    config: MeanReversionConfig,
) -> float:
    """根据信号族计算当前 Bar 的信号原始值。"""
    if config.family is SignalFamily.Z_SCORE:
        return compute_zscore(closes, config.lookback)
    elif config.family is SignalFamily.BOLLINGER:
        return compute_bollinger_position(
            closes, config.lookback, config.bollinger_num_std,
        )
    elif config.family is SignalFamily.RSI:
        return compute_rsi(closes, config.rsi_period)
    else:  # REVERSAL
        return compute_reversal(closes, config.lookback)


def _signal_direction(
    value: float,
    config: MeanReversionConfig,
) -> SignalDirection:
    """根据信号值和阈值判断入场/出场方向。

    所有家族统一使用"越低越超卖"逻辑:
    - Z_SCORE: z < -entry → 超卖入场;z > -exit → 回归出场
    - BOLLINGER: %B < entry → 接近下轨;%B > exit → 回到中间
    - RSI: RSI < entry → 超卖;RSI > exit → 回升
    - REVERSAL: ret < -entry → 急跌;ret > -exit → 回升
    """
    if value != value:  # NaN
        return SignalDirection(enter=False, exit=False)

    if config.family is SignalFamily.Z_SCORE:
        enter = value < -config.entry_threshold
        exit_ = value > -config.exit_threshold
    elif config.family is SignalFamily.BOLLINGER or config.family is SignalFamily.RSI:
        enter = value < config.entry_threshold
        exit_ = value > config.exit_threshold
    else:  # REVERSAL
        enter = value < -config.entry_threshold
        exit_ = value > -config.exit_threshold

    return SignalDirection(enter=enter, exit=exit_)


def generate_signals(
    closes: Mapping[str, Sequence[float]],
    config: MeanReversionConfig,
) -> list[SignalResult]:
    """对所有标的计算均值回归信号。

    :param closes: ``{symbol: [close_1, close_2, ...]}``,按时间排列。
    :param config: 策略配置。
    :return: 按 symbol 排序的信号列表,保证确定性。
    """
    results: list[SignalResult] = []
    for symbol in sorted(closes.keys()):
        prices = closes[symbol]
        value = _compute_signal_value(prices, config)
        direction = _signal_direction(value, config)
        results.append(SignalResult(
            symbol=symbol,
            value=value,
            should_enter=direction.enter,
            should_exit=direction.exit,
            family=config.family.value,
        ))
    return results
