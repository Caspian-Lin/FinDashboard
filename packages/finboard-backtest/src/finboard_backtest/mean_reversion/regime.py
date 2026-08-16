"""趋势 / 波动状态过滤 — 避免在单边下跌中持续补仓。

均值回归策略在趋势行情中容易失效(持续单边运动导致"抄底"不断亏损)。
状态过滤使用**仅历史数据**判断当前市场状态,在不利状态中禁止入场。

* **BULL** — 价格在 SMA(N) 之上,波动正常 → 允许入场。
* **BEAR** — 价格在 SMA(N) 之下 → 禁止入场。
* **HIGH_VOL** — 当前波动率 / 历史均值波动 > 阈值 → 禁止入场(不确定性过高)。
* **NEUTRAL** — 价格接近 SMA,波动正常 → 允许入场。

**未知状态不交易**:数据不足以计算状态时,标记为 UNKNOWN → 禁止入场。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    RegimeMethod,
)


class RegimeState(StrEnum):
    """市场状态分类。"""

    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    HIGH_VOL = "high_vol"
    UNKNOWN = "unknown"
    """数据不足以判断状态 → 不交易。"""


@dataclass(frozen=True, slots=True)
class RegimeResult:
    """单标的的市场状态判断结果。"""

    symbol: str
    state: RegimeState
    sma_ratio: float
    """close / SMA 比值;NaN 表示数据不足。"""
    vol_ratio: float
    """当前波动率 / 历史均值波动;NaN 表示数据不足。"""


def _sma(values: Sequence[float], window: int) -> float:
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def _realized_vol(prices: Sequence[float], window: int) -> float:
    """已实现波动率(日收益率标准差)。"""
    if len(prices) < window + 1:
        return float("nan")
    returns: list[float] = []
    for i in range(len(prices) - window, len(prices)):
        if prices[i - 1] > 0:
            returns.append(prices[i] / prices[i - 1] - 1.0)
    if not returns:
        return float("nan")
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(var)


def classify_regime(
    closes: Mapping[str, Sequence[float]],
    config: MeanReversionConfig,
) -> dict[str, RegimeResult]:
    """对每只标的判断当前市场状态。

    :param closes: ``{symbol: [close_1, ...]}``。
    :param config: 策略配置。
    :return: ``{symbol: RegimeResult}``,数据不足的标记为 UNKNOWN。
    """
    results: dict[str, RegimeResult] = {}

    for symbol, prices in closes.items():
        if config.regime_method is RegimeMethod.NONE:
            results[symbol] = RegimeResult(
                symbol=symbol,
                state=RegimeState.BULL,
                sma_ratio=float("nan"),
                vol_ratio=float("nan"),
            )
            continue

        sma = _sma(prices, config.regime_sma_window)
        if sma != sma or sma <= 0:
            results[symbol] = RegimeResult(
                symbol=symbol,
                state=RegimeState.UNKNOWN,
                sma_ratio=float("nan"),
                vol_ratio=float("nan"),
            )
            continue

        sma_ratio = prices[-1] / sma

        current_vol = _realized_vol(prices, min(20, len(prices) - 1))
        historical_vol = _realized_vol(prices, config.regime_vol_lookback)

        if current_vol != current_vol or historical_vol != historical_vol:
            vol_ratio = float("nan")
        elif historical_vol > 0:
            vol_ratio = current_vol / historical_vol
        else:
            vol_ratio = float("nan")

        if vol_ratio == vol_ratio and vol_ratio > config.regime_vol_max_ratio:
            state = RegimeState.HIGH_VOL
        elif sma_ratio > 1.02:
            state = RegimeState.BULL
        elif sma_ratio < 0.98:
            state = RegimeState.BEAR
        else:
            state = RegimeState.NEUTRAL

        results[symbol] = RegimeResult(
            symbol=symbol,
            state=state,
            sma_ratio=sma_ratio,
            vol_ratio=vol_ratio,
        )

    return results


def is_entry_allowed(regime: RegimeResult) -> bool:
    """判断当前状态是否允许入场。

    **未知状态不交易**:UNKNOWN / BEAR / HIGH_VOL 均禁止入场。
    """
    return regime.state in (RegimeState.BULL, RegimeState.NEUTRAL)
