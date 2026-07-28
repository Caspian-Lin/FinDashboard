"""ETF 轮动策略绩效分析。

输出:
* 资产类别暴露(时间序列均值)
* 风险贡献(按资产大类分解)
* 平均换手率
* 成本估计(佣金 + 印花税 + 滑点)
* 最大回撤持续期
* 资金档位可行性(10万/20万/50万元)
* 平均持仓数

所有分析均基于回测后的权重历史和收益序列,**不含前瞻数据**。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from finboard_backtest.etf_rotation.config import EtfRotationConfig
from finboard_backtest.etf_rotation.universe import EtfUniverse

CAPITAL_TIERS: tuple[float, ...] = (1e5, 2e5, 5e5)
"""资金档位:10万 / 20万 / 50万元。"""

TIER_LABELS: dict[float, str] = {1e5: "100k", 2e5: "200k", 5e5: "500k"}


@dataclass(frozen=True, slots=True)
class EtfRotationAnalysis:
    """ETF 轮动策略分析报告。"""

    asset_class_exposure: dict[str, float]
    """各资产大类的平均权重 {asset_class: avg_weight}。"""
    risk_contribution: dict[str, float]
    """各资产大类的风险贡献 {asset_class: rc}。"""
    avg_turnover: float
    """平均单边换手率(每次调仓)。"""
    total_cost_estimate: float
    """估算总成本占初始资金比例。"""
    max_drawdown_duration_days: int
    """最大回撤持续期(日历日近似)。"""
    capital_tier_feasibility: dict[str, bool]
    """各资金档位是否可行(最小持仓手数 > 0)。"""
    avg_n_holdings: float
    """平均持仓 ETF 数量。"""
    n_rebalances: int
    """调仓次数。"""
    n_flight_to_safety: int
    """触发避险切换的次数。"""


def compute_turnover(
    prev_weights: dict[str, float],
    new_weights: dict[str, float],
) -> float:
    """计算单边换手率 = Σ|Δw| / 2。"""
    all_symbols = set(prev_weights.keys()) | set(new_weights.keys())
    total = 0.0
    for sym in all_symbols:
        total += abs(new_weights.get(sym, 0.0) - prev_weights.get(sym, 0.0))
    return total / 2.0


def compute_asset_class_exposure(
    weight_history: Sequence[dict[str, float]],
    universe: EtfUniverse,
) -> dict[str, float]:
    """计算各资产大类的平均权重。"""
    if not weight_history:
        return {}
    accum: dict[str, float] = {}
    for weights in weight_history:
        for sym, w in weights.items():
            member = universe.get(sym)
            ac = member.asset_class.value if member is not None else "unknown"
            accum[ac] = accum.get(ac, 0.0) + w
    return {ac: total / len(weight_history) for ac, total in accum.items()}


def compute_risk_contribution(
    weights: dict[str, float],
    closes: Mapping[str, Sequence[float]],
    universe: EtfUniverse,
    vol_lookback: int = 60,
) -> dict[str, float]:
    """按资产大类计算风险贡献。

    RC_i = w_i * sigma_i / sum(w_j * sigma_j)

    先按单个 ETF 计算边际风险贡献,然后汇总到大类。
    """
    if not weights:
        return {}

    contributions: dict[str, float] = {}
    for sym, w in weights.items():
        if w <= 0:
            continue
        prices = closes.get(sym)
        if prices is None or len(prices) < vol_lookback + 1:
            continue
        returns = _daily_returns(prices[-(vol_lookback + 1):])
        vol = _std_dev(returns)
        if vol != vol or vol <= 0:
            continue
        member = universe.get(sym)
        ac = member.asset_class.value if member is not None else "unknown"
        contributions[ac] = contributions.get(ac, 0.0) + w * vol

    total = sum(contributions.values())
    if total <= 0:
        return {}
    return {ac: rc / total for ac, rc in contributions.items()}


def check_capital_tier_feasibility(
    weights: dict[str, float],
    universe: EtfUniverse,
    capital: float,
) -> bool:
    """检查给定资金档位下最小手数是否可行。

    每只 ETF 的目标金额 = capital * weight,必须 >= lot_size * min_price。
    这里用保守估计:min_price = 1.0 元。
    """
    for sym, w in weights.items():
        if w <= 0:
            continue
        member = universe.get(sym)
        lot = member.lot_size if member is not None else 100
        target_value = capital * w
        min_value = lot * 1.0  # 保守估计最小价格 1 元
        if target_value < min_value:
            return False
    return True


def compute_max_drawdown_duration(
    equity_curve: Sequence[float],
    dates: Sequence[object] | None = None,
) -> int:
    """计算最大回撤持续期(日数)。

    从前一个历史高点到回撤结束(创新高或回撤结束)的最长天数。
    回撤中的每一天都计入持续期。
    """
    if len(equity_curve) < 2:
        return 0

    peak = equity_curve[0]
    max_duration = 0
    in_drawdown = False
    dd_start = 0

    for i in range(1, len(equity_curve)):
        if equity_curve[i] >= peak:
            if in_drawdown:
                duration = i - dd_start
                if duration > max_duration:
                    max_duration = duration
                in_drawdown = False
            peak = equity_curve[i]
        else:
            if not in_drawdown:
                in_drawdown = True
                dd_start = i
            else:
                duration = i - dd_start + 1
                if duration > max_duration:
                    max_duration = duration

    if in_drawdown:
        duration = len(equity_curve) - dd_start
        if duration > max_duration:
            max_duration = duration

    return max_duration


def analyze_etf_rotation(
    weight_history: Sequence[dict[str, float]],
    closes: Mapping[str, Sequence[float]],
    equity_curve: Sequence[float],
    universe: EtfUniverse,
    config: EtfRotationConfig,
    *,
    flight_to_safety_count: int = 0,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
) -> EtfRotationAnalysis:
    """完整的 ETF 轮动策略绩效分析。

    :param weight_history: 每次调仓后的权重快照列表。
    :param closes: 各 ETF 的收盘价序列。
    :param equity_curve: 策略净值曲线(等间距)。
    :param universe: ETF 候选池。
    :param config: 策略配置。
    :param flight_to_safety_count: 触发避险的次数。
    :param commission_rate: 佣金费率(默认万 3)。
    :param stamp_tax_rate: 印花税费率(默认千 1)。
    """
    avg_exposure = compute_asset_class_exposure(weight_history, universe)

    last_weights = weight_history[-1] if weight_history else {}
    risk_contrib = compute_risk_contribution(
        last_weights, closes, universe, config.vol_lookback,
    )

    turnovers: list[float] = []
    for i in range(1, len(weight_history)):
        t = compute_turnover(weight_history[i - 1], weight_history[i])
        turnovers.append(t)
    avg_turnover = sum(turnovers) / len(turnovers) if turnovers else 0.0

    total_cost = sum(turnovers) * (commission_rate + stamp_tax_rate)

    dd_duration = compute_max_drawdown_duration(equity_curve)

    tier_feasibility: dict[str, bool] = {}
    for capital in CAPITAL_TIERS:
        label = TIER_LABELS.get(capital, str(capital))
        tier_feasibility[label] = check_capital_tier_feasibility(
            last_weights, universe, capital,
        )

    avg_holdings = (
        sum(len(w) for w in weight_history) / len(weight_history)
        if weight_history else 0.0
    )

    return EtfRotationAnalysis(
        asset_class_exposure=avg_exposure,
        risk_contribution=risk_contrib,
        avg_turnover=avg_turnover,
        total_cost_estimate=total_cost,
        max_drawdown_duration_days=dd_duration,
        capital_tier_feasibility=tier_feasibility,
        avg_n_holdings=avg_holdings,
        n_rebalances=max(len(weight_history) - 1, 0),
        n_flight_to_safety=flight_to_safety_count,
    )


def _daily_returns(closes: Sequence[float]) -> list[float]:
    if len(closes) < 2:
        return []
    returns: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            returns.append(closes[i] / prev - 1.0)
    return returns


def _std_dev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)
