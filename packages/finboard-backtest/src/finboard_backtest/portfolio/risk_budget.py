"""风险预算层 —— 目标波动率缩放 / 波动率上限 / 再平衡带。

issue #59 要求:
1. 组合级目标波动率:当实际波动率与目标不同时,按比例缩放权重并
   补充/释放现金,**只用回看窗口已知数据**。
2. 波动率上限:超限时截断权重到安全水平。
3. 再平衡带(hysteresis):权重微小变化不触发交易,避免高频调仓。
4. 相关性集中度:单一资产/ sleeve 的风险贡献超限时告警(不自动修正,
   由上层决定是否收紧约束)。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    PortfolioConstraints,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate

ANNUALIZATION_FACTOR = 252 ** 0.5


@dataclass(frozen=True, slots=True)
class VolScalingResult:
    """波动率缩放结果。

    属性:
        scaled_weights: 缩放后的权重向量。
        realized_volatility: 缩放前的组合年化波动率。
        target_volatility: 目标年化波动率。
        scaling_factor: 缩放系数 = target / realized(>1 加仓,<1 减仓)。
        capped: 是否被 max_volatility 截断。
    """

    scaled_weights: dict[str, float]
    realized_volatility: float
    target_volatility: float
    scaling_factor: float
    capped: bool


class RiskBudgetError(RuntimeError):
    """风险预算计算失败。"""


def portfolio_volatility(
    weights: dict[str, float],
    covariance: CovarianceEstimate,
    *,
    annualized: bool = True,
) -> float:
    """计算组合年化波动率。

    sigma_p = sqrt(w' Sigma w)
    """
    if not weights:
        return 0.0

    tickers = covariance.tickers
    idx_map = {t: i for i, t in enumerate(tickers)}

    w_vec = np.zeros(len(tickers))
    for code, weight in weights.items():
        if code in idx_map and weight > 0:
            w_vec[idx_map[code]] = weight

    var = float(w_vec @ covariance.matrix @ w_vec)
    if var < 0:
        var = 0.0
    vol = float(var ** 0.5)
    if annualized:
        vol *= ANNUALIZATION_FACTOR
    return vol


def scale_to_target_volatility(
    target: TargetWeight,
    covariance: CovarianceEstimate,
    constraints: PortfolioConstraints,
) -> TargetWeight:
    """按目标波动率缩放权重。

    若 ``constraints.target_volatility`` 为 None,直接返回原权重。

    缩放逻辑:
    1. 计算当前组合年化波动率 sigma_p。
    2. 缩放系数 k = target_vol / sigma_p。
    3. 若 k > 1 但 max_leverage=1.0,无法加杠杆 → 权重不变,现金不变。
    4. 若 k < 1,减仓释放现金 → 权重 *= k,cash += (1-k)*gross。
    5. 若实际波动率 > max_volatility,强制截断到 max。
    """
    if constraints.target_volatility is None and constraints.max_volatility is None:
        return target

    if not target.weights:
        return target

    realized = portfolio_volatility(target.weights, covariance, annualized=True)
    if realized <= MAX_WEIGHT_EPSILON:
        return target

    target_vol = constraints.target_volatility
    cap_vol = constraints.max_volatility

    effective_target = target_vol
    if cap_vol is not None and realized > cap_vol:
        effective_target = cap_vol
    if effective_target is None:
        return target

    scaling = effective_target / realized

    max_gross = constraints.max_investable_weight
    current_gross = sum(target.weights.values())
    if current_gross <= MAX_WEIGHT_EPSILON:
        return target

    max_scaling = max_gross / current_gross if max_gross > 0 else 1.0
    scaling = min(scaling, max_scaling)

    if scaling >= 1.0 - MAX_WEIGHT_EPSILON:
        return target

    scaled_weights = {c: w * scaling for c, w in target.weights.items()}
    scaled_gross = sum(scaled_weights.values())
    new_cash = target.cash_buffer + (current_gross - scaled_gross)

    return TargetWeight(
        weights=scaled_weights,
        as_of=target.as_of,
        strategy_id=target.strategy_id,
        cash_buffer=new_cash,
        contract_version=target.contract_version,
        factor_snapshot_id=target.factor_snapshot_id,
        covariance_version=target.covariance_version,
    )


def needs_rebalance(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    constraints: PortfolioConstraints,
) -> bool:
    """判断是否需要再平衡(基于 hysteresis band)。

    当任一标的的权重偏差超过 ``rebalance_threshold`` 时才触发调仓。
    这避免了因微小信号变化频繁交易。
    """
    all_codes = set(current_weights) | set(target_weights)
    for code in all_codes:
        diff = abs(target_weights.get(code, 0.0) - current_weights.get(code, 0.0))
        if diff > constraints.rebalance_threshold:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ConcentrationCheck:
    """集中度检查结果。"""

    max_risk_contribution: float
    argmax_code: str
    passed: bool
    threshold: float


def risk_concentration_check(
    weights: dict[str, float],
    covariance: CovarianceEstimate,
    *,
    threshold: float = 0.30,
) -> ConcentrationCheck:
    """检查单一标的风险贡献是否超限。

    ERC 的理想状态是每个标的贡献 1/N 的风险;实际中单标的贡献超过
    ``threshold`` 时告警。返回 ``ConcentrationCheck``,不自动修正。
    """
    if not weights:
        return ConcentrationCheck(
            max_risk_contribution=0.0,
            argmax_code="",
            passed=True,
            threshold=threshold,
        )

    tickers = covariance.tickers
    idx_map = {t: i for i, t in enumerate(tickers)}

    w_vec = np.zeros(len(tickers))
    for code, weight in weights.items():
        if code in idx_map:
            w_vec[idx_map[code]] = weight

    var = float(w_vec @ covariance.matrix @ w_vec)
    if var <= MAX_WEIGHT_EPSILON:
        return ConcentrationCheck(
            max_risk_contribution=0.0,
            argmax_code="",
            passed=True,
            threshold=threshold,
        )

    mrc: npt.NDArray[np.float64] = covariance.matrix @ w_vec
    rc = w_vec * mrc
    total_rc = rc.sum()
    if total_rc <= MAX_WEIGHT_EPSILON:
        return ConcentrationCheck(
            max_risk_contribution=0.0,
            argmax_code="",
            passed=True,
            threshold=threshold,
        )

    rc_pct = rc / total_rc

    active_indices = [i for i, c in enumerate(tickers) if weights.get(c, 0) > 0]
    if not active_indices:
        return ConcentrationCheck(
            max_risk_contribution=0.0,
            argmax_code="",
            passed=True,
            threshold=threshold,
        )

    max_idx = max(active_indices, key=lambda i: rc_pct[i])
    max_rc = float(rc_pct[max_idx])
    max_code = tickers[max_idx]

    return ConcentrationCheck(
        max_risk_contribution=max_rc,
        argmax_code=max_code,
        passed=max_rc <= threshold + MAX_WEIGHT_EPSILON,
        threshold=threshold,
    )


__all__ = [
    "ANNUALIZATION_FACTOR",
    "ConcentrationCheck",
    "RiskBudgetError",
    "VolScalingResult",
    "needs_rebalance",
    "portfolio_volatility",
    "risk_concentration_check",
    "scale_to_target_volatility",
]
