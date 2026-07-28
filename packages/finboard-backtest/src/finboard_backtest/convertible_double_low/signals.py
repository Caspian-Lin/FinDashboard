"""可转债双低排名信号(issue #63)。

信号设计:
* 基础:双低 = price + premium * 100,取最小的 ``top_n`` 只。
* 扩展:可加权 YTM、剩余期限、流动性因子;权重在 Config 中预注册。
* PIT:仅使用 ``as_of`` 当日收盘后可知的数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finboard_backtest.convertible_double_low.config import (
    ConvertibleDoubleLowConfig,
    FactorWeight,
)
from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot


@dataclass(frozen=True, slots=True)
class ConvertibleSignal:
    """单只可转债的排名信号输出。"""

    code: str
    double_low_value: Decimal
    composite_score: Decimal
    rank: int


def _percentile_rank(
    value: Decimal,
    all_values: list[Decimal],
) -> Decimal:
    """计算 ``value`` 在 ``all_values`` 中的百分位排名(0=最小, 1=最大)。

    值越小排名越靠前(双低越小越好)。
    """
    if len(all_values) <= 1:
        return Decimal("0")
    sorted_vals = sorted(all_values)
    idx = 0
    for v in sorted_vals:
        if v < value:
            idx += 1
    return Decimal(idx) / Decimal(len(all_values) - 1)


def compute_composite_score(
    snapshot: ConvertibleSnapshot,
    all_snapshots: list[ConvertibleSnapshot],
    config: ConvertibleDoubleLowConfig,
) -> Decimal:
    """计算加权综合得分(越小越好)。

    每个因子先做百分位排名(0~1),再按预注册权重加权。
    """
    weights = config.factor_weights
    total_weight = sum(weights.values(), Decimal("0"))
    if total_weight == 0:
        return snapshot.double_low_value

    prices = [s.close for s in all_snapshots]
    premiums = [s.conversion_premium for s in all_snapshots]
    ytms = [s.ytm or Decimal("0") for s in all_snapshots]
    durations = [
        Decimal(s.days_to_maturity or 0) for s in all_snapshots
    ]
    liquidities = [s.avg_amount_20d for s in all_snapshots]

    score = Decimal("0")

    w = weights.get(FactorWeight.PRICE, Decimal("0"))
    if w > 0:
        score += w * _percentile_rank(snapshot.close, prices)

    w = weights.get(FactorWeight.PREMIUM, Decimal("0"))
    if w > 0:
        score += w * _percentile_rank(snapshot.conversion_premium, premiums)

    w = weights.get(FactorWeight.YTM, Decimal("0"))
    if w > 0:
        score += w * (Decimal("1") - _percentile_rank(snapshot.ytm or Decimal("0"), ytms))

    w = weights.get(FactorWeight.DURATION, Decimal("0"))
    if w > 0:
        score += w * _percentile_rank(
            Decimal(snapshot.days_to_maturity or 0), durations
        )

    w = weights.get(FactorWeight.LIQUIDITY, Decimal("0"))
    if w > 0:
        score += w * (Decimal("1") - _percentile_rank(snapshot.avg_amount_20d, liquidities))

    return score / total_weight


def generate_signals(
    candidates: list[ConvertibleSnapshot],
    config: ConvertibleDoubleLowConfig,
) -> list[ConvertibleSignal]:
    """对候选池生成排名信号,返回按综合得分升序排列的结果。

    仅返回前 ``top_n`` 只(经进入缓冲扩展后的完整列表)。
    """
    if not candidates:
        return []

    ranked: list[tuple[Decimal, ConvertibleSnapshot]] = []
    for snap in candidates:
        score = compute_composite_score(snap, candidates, config)
        ranked.append((score, snap))

    ranked.sort(key=lambda x: x[0])

    top_n = config.top_n
    signals: list[ConvertibleSignal] = []
    for i, (score, snap) in enumerate(ranked[:top_n]):
        signals.append(
            ConvertibleSignal(
                code=snap.code,
                double_low_value=snap.double_low_value,
                composite_score=score,
                rank=i + 1,
            )
        )
    return signals


def compute_rank_threshold(
    config: ConvertibleDoubleLowConfig,
    *,
    direction: str,
) -> int:
    """计算进入/退出排名阈值。

    * ``direction="entry"``: ``top_n * (1 + entry_buffer_pct)``
    * ``direction="exit"``: ``top_n * (1 + exit_buffer_pct)``
    """
    if direction == "entry":
        ratio = Decimal("1") + config.entry_buffer_pct
    else:
        ratio = Decimal("1") + config.exit_buffer_pct
    return max(1, int(config.top_n * ratio))


__all__ = [
    "ConvertibleSignal",
    "compute_composite_score",
    "compute_rank_threshold",
    "generate_signals",
]
