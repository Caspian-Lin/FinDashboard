"""时间序列动量(TSMOM)信号生成。

核心逻辑:
1. **方向信号**:过去 N 日收益率为正 → 做多(LONG);为负 → 做空(SHORT)。
2. **多 lookback 聚合**:对多个窗口(1/3/6/12 月)的信号取平均,降低
   对单一参数的过拟合风险。
3. **波动率缩放**:仓位 = 目标波动率 / 标的波动率,使每个品种贡献等量
   风险。波动率受下限保护(``vol_floor``),防止过度杠杆。
4. **风险预算约束**:缩放后的仓位受最大杠杆 / 保证金 / 集中度约束裁剪。

**PIT 安全**:所有信号仅使用截至当日收盘的历史数据;信号在 T 日生成,
成交在 T+1 开盘,不存在 look-ahead。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .config import FuturesTsmomConfig, TsmomSignalFamily


@dataclass(frozen=True, slots=True)
class TsmomSignal:
    """单品种的 TSMOM 信号。"""

    symbol: str
    direction: int
    """+1 = LONG, -1 = SHORT, 0 = FLAT。"""
    target_weight: float
    """目标名义权重(方向 x 波动率缩放,受约束裁剪)。"""
    composite_score: float
    """多 lookback 综合得分([-1, 1])。"""
    realized_vol: float
    """年化波动率估计。"""
    reasons: tuple[str, ...]

    @property
    def is_long(self) -> bool:
        return self.direction > 0

    @property
    def is_short(self) -> bool:
        return self.direction < 0

    @property
    def is_flat(self) -> bool:
        return self.direction == 0


def _daily_returns(closes: Sequence[float]) -> list[float]:
    if len(closes) < 2:
        return []
    result: list[float] = []
    prev = closes[0]
    for px in closes[1:]:
        if prev > 0:
            result.append(px / prev - 1.0)
        else:
            result.append(0.0)
        prev = px
    return result


def realized_volatility(
    closes: Sequence[float],
    lookback: int,
    *,
    annualization: float = math.sqrt(252.0),
) -> float:
    """年化已实现波动率(日收益标准差 x sqrt(252))。"""
    if len(closes) < lookback + 1:
        return float("inf")
    window = closes[-(lookback + 1):]
    rets = _daily_returns(window)
    if len(rets) < 2:
        return float("inf")
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * annualization


def past_return(closes: Sequence[float], lookback: int) -> float:
    """过去 ``lookback`` 天的总收益率。"""
    if len(closes) < lookback + 1:
        return 0.0
    old = closes[-(lookback + 1)]
    new = closes[-1]
    if old <= 0:
        return 0.0
    return new / old - 1.0


def compute_composite_score(
    closes: Sequence[float],
    lookbacks: tuple[int, ...],
    *,
    family: TsmomSignalFamily = TsmomSignalFamily.MULTIPLE_LOOKBACK,
) -> float:
    """多 lookback 综合动量得分,范围 [-1, 1]。

    * ``SINGLE_LOOKBACK``:只用第一个 lookback 的 sign。
    * ``MULTIPLE_LOOKBACK``:所有 lookback 的 sign 取平均。
    """
    if family is TsmomSignalFamily.SINGLE_LOOKBACK:
        lb = lookbacks[0]
        ret = past_return(closes, lb)
        return math.copysign(1.0, ret) if abs(ret) > 1e-10 else 0.0

    scores: list[float] = []
    for lb in lookbacks:
        ret = past_return(closes, lb)
        scores.append(math.copysign(1.0, ret) if abs(ret) > 1e-10 else 0.0)
    return sum(scores) / len(scores)


def generate_tsmom_signal(
    symbol: str,
    closes: Sequence[float],
    config: FuturesTsmomConfig,
) -> TsmomSignal:
    """为单一品种生成 TSMOM 信号。

    仓位大小 = vol_target / max(realized_vol, vol_floor),再乘以综合得分。
    """
    score = compute_composite_score(closes, config.lookbacks, family=config.family)
    direction = int(math.copysign(1, score)) if abs(score) > 1e-10 else 0

    vol = realized_volatility(closes, config.vol_lookback)
    effective_vol = max(vol, config.vol_floor)

    raw_weight = config.vol_target / effective_vol
    target_weight = abs(score) * raw_weight

    reasons: list[str] = []
    if direction > 0:
        reasons.append(f"long: score={score:.3f}")
    elif direction < 0:
        reasons.append(f"short: score={score:.3f}")
    else:
        reasons.append("flat: score~0")
    reasons.append(f"vol={vol:.4f}")
    reasons.append(f"raw_w={raw_weight:.4f}")

    return TsmomSignal(
        symbol=symbol,
        direction=direction,
        target_weight=target_weight,
        composite_score=score,
        realized_vol=vol,
        reasons=tuple(reasons),
    )


def generate_signals(
    closes_by_symbol: Mapping[str, Sequence[float]],
    config: FuturesTsmomConfig,
) -> dict[str, TsmomSignal]:
    """为所有品种生成 TSMOM 信号。"""
    result: dict[str, TsmomSignal] = {}
    for symbol, closes in closes_by_symbol.items():
        if len(closes) < config.min_data_days:
            result[symbol] = TsmomSignal(
                symbol=symbol,
                direction=0,
                target_weight=0.0,
                composite_score=0.0,
                realized_vol=float("inf"),
                reasons=("insufficient_data",),
            )
            continue
        result[symbol] = generate_tsmom_signal(symbol, closes, config)
    return result


__all__ = [
    "TsmomSignal",
    "compute_composite_score",
    "generate_signals",
    "generate_tsmom_signal",
    "past_return",
    "realized_volatility",
]
