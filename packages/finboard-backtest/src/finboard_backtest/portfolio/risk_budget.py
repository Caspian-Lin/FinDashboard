"""风险预算层 —— 目标波动率缩放 / 波动率上限 / 再平衡带。

issue #59 要求:
1. 组合级目标波动率:当实际波动率与目标不同时,按比例缩放权重并
   补充/释放现金,**只用回看窗口已知数据**。
2. 波动率上限:超限时截断权重到安全水平。
3. 再平衡带(hysteresis):权重微小变化不触发交易,避免高频调仓。
4. 相关性集中度:单一资产风险贡献可执行硬上限;投影只会降低权重,
   不会为了满足风险贡献指标而暗中增加其它资产敞口。
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

ANNUALIZATION_FACTOR = 252**0.5


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


@dataclass(frozen=True, slots=True)
class RiskContributionProjection:
    """单资产风险贡献硬上限的确定性投影结果。"""

    weights: dict[str, float]
    before_max_contribution: float
    after_max_contribution: float
    before_argmax: str
    after_argmax: str
    iterations: int
    converged: bool


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
        if code in idx_map:
            w_vec[idx_map[code]] = weight

    var = float(w_vec @ covariance.matrix @ w_vec)
    if var < 0:
        var = 0.0
    vol = float(var**0.5)
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
    3. 若 k > 1,最多使用现有现金加仓到 gross/现金共同上限;显式杠杆组合
       最多加到 ``max_leverage``。
    4. 若 k < 1,减仓释放现金。
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
    current_gross = sum(abs(weight) for weight in target.weights.values())
    if current_gross <= MAX_WEIGHT_EPSILON:
        return target

    max_scaling = max_gross / current_gross if max_gross > 0 else 1.0
    scaling = min(scaling, max_scaling)

    if abs(scaling - 1.0) <= MAX_WEIGHT_EPSILON:
        return target

    scaled_weights = {c: w * scaling for c, w in target.weights.items()}
    scaled_gross = sum(abs(weight) for weight in scaled_weights.values())
    if constraints.long_only and constraints.max_leverage <= 1 + MAX_WEIGHT_EPSILON:
        new_cash = max(constraints.min_cash_buffer, 1.0 - scaled_gross)
    else:
        new_cash = max(constraints.min_cash_buffer, target.cash_buffer)

    return TargetWeight(
        weights=scaled_weights,
        as_of=target.as_of,
        strategy_id=target.strategy_id,
        cash_buffer=new_cash,
        max_leverage=constraints.max_leverage,
        long_only=constraints.long_only,
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


def enforce_risk_contribution_cap(
    weights: dict[str, float],
    covariance: CovarianceEstimate,
    *,
    threshold: float,
    max_iterations: int = 1000,
    tolerance: float = 1e-8,
) -> RiskContributionProjection:
    """把单资产风险贡献投影到硬上限内。

    该求解器面向通用 long-only 组合。每轮只降低风险贡献超限资产的权重,
    不把释放权重重新分配给其它资产;因此资产/sleeve/gross 上限仍保持成立。
    风险贡献占比对统一缩放不敏感,不收敛或理论上不可行时必须失败关闭。
    """
    if not 0 < threshold <= 1:
        raise RiskBudgetError("风险贡献阈值必须落在 (0, 1]")
    if max_iterations <= 0:
        raise RiskBudgetError("max_iterations 必须为正")
    if tolerance <= 0:
        raise RiskBudgetError("tolerance 必须为正")
    if not weights:
        return RiskContributionProjection(
            weights={},
            before_max_contribution=0.0,
            after_max_contribution=0.0,
            before_argmax="",
            after_argmax="",
            iterations=0,
            converged=True,
        )
    if any(weight < -MAX_WEIGHT_EPSILON for weight in weights.values()):
        raise RiskBudgetError("风险贡献硬约束当前仅支持 long-only 目标")

    missing = sorted(set(weights) - set(covariance.tickers))
    if missing:
        raise RiskBudgetError(f"协方差缺少目标标的: {missing}")
    active = {
        symbol: float(weight)
        for symbol, weight in weights.items()
        if weight > MAX_WEIGHT_EPSILON
    }
    if not active:
        return RiskContributionProjection(
            weights={},
            before_max_contribution=0.0,
            after_max_contribution=0.0,
            before_argmax="",
            after_argmax="",
            iterations=0,
            converged=True,
        )
    if threshold + tolerance < 1.0 / len(active):
        raise RiskBudgetError(
            "风险贡献上限不可行:"
            f" threshold={threshold:.6f} < 1/n={1.0 / len(active):.6f}"
        )

    index = {ticker: idx for idx, ticker in enumerate(covariance.tickers)}

    def concentration(
        candidate: dict[str, float],
    ) -> tuple[float, str, npt.NDArray[np.float64]]:
        vector = np.zeros(len(covariance.tickers), dtype=np.float64)
        for symbol, weight in candidate.items():
            vector[index[symbol]] = weight
        variance = float(vector @ covariance.matrix @ vector)
        if not np.isfinite(variance) or variance <= MAX_WEIGHT_EPSILON:
            raise RiskBudgetError(
                f"组合方差必须为正且有限,实际为 {variance}"
            )
        raw = vector * (covariance.matrix @ vector)
        ratios = np.asarray(raw / variance, dtype=np.float64)
        if not np.isfinite(ratios).all():
            raise RiskBudgetError("风险贡献包含非有限值")
        active_symbols = sorted(candidate)
        argmax = max(active_symbols, key=lambda symbol: float(ratios[index[symbol]]))
        return float(ratios[index[argmax]]), argmax, ratios

    before_max, before_argmax, ratios = concentration(active)
    if before_max <= threshold + tolerance:
        return RiskContributionProjection(
            weights=active,
            before_max_contribution=before_max,
            after_max_contribution=before_max,
            before_argmax=before_argmax,
            after_argmax=before_argmax,
            iterations=0,
            converged=True,
        )

    projected = dict(active)
    for iteration in range(1, max_iterations + 1):
        over_limit = [
            symbol
            for symbol in sorted(projected)
            if float(ratios[index[symbol]]) > threshold + tolerance
        ]
        if not over_limit:
            after_max, after_argmax, _ = concentration(projected)
            return RiskContributionProjection(
                weights=projected,
                before_max_contribution=before_max,
                after_max_contribution=after_max,
                before_argmax=before_argmax,
                after_argmax=after_argmax,
                iterations=iteration - 1,
                converged=True,
            )
        for symbol in over_limit:
            contribution = float(ratios[index[symbol]])
            factor = float(np.sqrt(threshold / contribution))
            # 保证每轮有确定性进展,同时避免一步把资产权重压到数值零。
            projected[symbol] *= min(0.99, max(0.10, factor))
        projected = {
            symbol: weight
            for symbol, weight in projected.items()
            if weight > MAX_WEIGHT_EPSILON
        }
        if not projected:
            raise RiskBudgetError("风险贡献投影把全部目标压缩为零")
        after_max, after_argmax, ratios = concentration(projected)
        if after_max <= threshold + tolerance:
            return RiskContributionProjection(
                weights=projected,
                before_max_contribution=before_max,
                after_max_contribution=after_max,
                before_argmax=before_argmax,
                after_argmax=after_argmax,
                iterations=iteration,
                converged=True,
            )

    after_max, after_argmax, _ = concentration(projected)
    raise RiskBudgetError(
        "风险贡献硬约束未收敛:"
        f" before={before_max:.6f}, after={after_max:.6f},"
        f" limit={threshold:.6f}, iterations={max_iterations},"
        f" argmax={after_argmax}"
    )


__all__ = [
    "ANNUALIZATION_FACTOR",
    "ConcentrationCheck",
    "RiskBudgetError",
    "RiskContributionProjection",
    "VolScalingResult",
    "enforce_risk_contribution_cap",
    "needs_rebalance",
    "portfolio_volatility",
    "risk_concentration_check",
    "scale_to_target_volatility",
]
