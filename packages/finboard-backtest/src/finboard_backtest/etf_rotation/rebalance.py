"""调仓调度与漂移检查。

* ``should_rebalance()`` — 根据调仓频率和权重漂移判断是否需要调仓。
* ``RebalanceDecision`` — 调仓决策结果,包含原因和漂移度量。

设计原则:
* 再平衡带(``rebalance_threshold``):权重偏离不超过阈值时不调仓,减少换手。
* 调仓频率(``rebalance_frequency``):即使没有漂移,达到频率间隔也应检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from finboard_backtest.etf_rotation.config import EtfRotationConfig


@dataclass(frozen=True, slots=True)
class RebalanceDecision:
    """调仓决策。"""

    should_rebalance: bool
    reason: str
    """``scheduled`` / ``drift_exceeded`` / ``no_change`` / ``insufficient_data``。"""
    max_drift: float
    """当前权重与目标权重的最大绝对偏差。"""
    days_since_last: int
    """距离上次调仓的交易日数。"""


def should_rebalance(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    last_rebalance_date: date,
    current_date: date,
    config: EtfRotationConfig,
    *,
    trading_days_between: int | None = None,
) -> RebalanceDecision:
    """判断是否应该调仓。

    :param current_weights: 当前持仓权重。
    :param target_weights: 目标权重(本次信号生成的)。
    :param last_rebalance_date: 上次调仓日期。
    :param current_date: 当前日期。
    :param config: 策略配置。
    :param trading_days_between: 两次日期间的交易日数(可选);如果为 None
        则用日历天数近似。
    """
    if trading_days_between is not None:
        days = trading_days_between
    else:
        days = (current_date - last_rebalance_date).days

    max_drift = _compute_max_drift(current_weights, target_weights)

    if days < config.rebalance_interval_days and max_drift <= config.rebalance_threshold:
        return RebalanceDecision(
            should_rebalance=False,
            reason="no_change",
            max_drift=max_drift,
            days_since_last=days,
        )

    if max_drift > config.rebalance_threshold:
        return RebalanceDecision(
            should_rebalance=True,
            reason="drift_exceeded",
            max_drift=max_drift,
            days_since_last=days,
        )

    if days >= config.rebalance_interval_days:
        return RebalanceDecision(
            should_rebalance=True,
            reason="scheduled",
            max_drift=max_drift,
            days_since_last=days,
        )

    return RebalanceDecision(
        should_rebalance=False,
        reason="no_change",
        max_drift=max_drift,
        days_since_last=days,
    )


def _compute_max_drift(
    current: dict[str, float],
    target: dict[str, float],
) -> float:
    """计算当前权重与目标权重的最大绝对偏差。

    新进入的标的(current 无)偏差为 target 权重;退出的标的(target 无)
    偏差为 current 权重。
    """
    all_symbols = set(current.keys()) | set(target.keys())
    max_drift = 0.0
    for sym in all_symbols:
        c = current.get(sym, 0.0)
        t = target.get(sym, 0.0)
        drift = abs(c - t)
        if drift > max_drift:
            max_drift = drift
    return max_drift
