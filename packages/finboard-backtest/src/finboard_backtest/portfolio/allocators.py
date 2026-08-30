"""组合分配算法 — 等权 / 逆波动率 / 等风险贡献(ERC)。

issue #59 要求所有方法支持权重上限、sleeve 上限、现金缓冲和禁用杠杆默认值。
三种方法在相同输入下确定性产出,权重 / 现金 / 杠杆和集中度约束始终成立。

核心安全约束:
1. 实际 gross exposure <= 配置 max_leverage(默认 1.0 = 无杠杆),现金独立保留。
2. 单标的权重 <= max_weight_per_asset。
3. 同 sleeve 标的权重之和 <= max_weight_per_sleeve。
4. 禁用/缺数标的权重 → 0(不放大其他标的)。
5. 协方差失败策略由统一 builder 显式选择 fail-safe 或 fail-closed。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    PortfolioConstraints,
    Signal,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate

DEFAULT_MAX_ITER = 500
DEFAULT_TOLERANCE = 1e-8


class AllocationError(RuntimeError):
    """分配失败 —— 无可用标的、约束不可行或数值异常。"""


@dataclass(frozen=True, slots=True)
class AllocationContext:
    """标的分组与可投资状态;缺失映射时每个标的单独归入 unclassified sleeve。"""

    sleeve_map: dict[str, str] = field(default_factory=dict)
    disabled_symbols: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ConstraintAdjustment:
    """单次约束投影的审计差异。"""

    constraint: str
    symbol: str | None
    before_value: float
    after_value: float
    limit: float | None
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ConstraintApplication:
    """约束投影结果及逐项 before/after。"""

    weights: dict[str, float]
    adjustments: tuple[ConstraintAdjustment, ...]


class Allocator(Protocol):
    """组合分配协议。

    分配器接收信号列表和(可选)协方差估计,返回满足 ``PortfolioConstraints``
    的目标权重。确定性输入 → 确定性输出,无随机成分。
    """

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
        *,
        context: AllocationContext | None = None,
    ) -> TargetWeight: ...


def _candidate_tickers(
    signals: list[Signal],
    covariance: CovarianceEstimate | None,
    constraints: PortfolioConstraints,
    context: AllocationContext,
) -> list[str]:
    """确定参与分配的标的列表。

    有协方差时取交集(信号 ∩ 协方差覆盖标的),确保波动率已知。
    无协方差时取信号涉及的标的。
    """
    signal_tickers = {
        signal.symbol
        for signal in signals
        if abs(signal.score * signal.confidence) > MAX_WEIGHT_EPSILON
        and signal.symbol not in context.disabled_symbols
        and (signal.score > 0 or not constraints.long_only)
    }
    if covariance is not None:
        return sorted(signal_tickers & set(covariance.tickers))
    return sorted(signal_tickers)


def apply_portfolio_constraints(
    weights: dict[str, float],
    constraints: PortfolioConstraints,
    *,
    context: AllocationContext | None = None,
) -> ConstraintApplication:
    """依次执行禁用、long-only、单资产、sleeve 与 gross 上限。

    每层只会维持或降低绝对权重,不会在 cap 后重新放大其它标的。
    """
    context = context or AllocationContext()
    if not weights:
        return ConstraintApplication(weights={}, adjustments=())

    adjusted = dict(weights)
    audit: list[ConstraintAdjustment] = []

    for code in sorted(adjusted):
        before = adjusted[code]
        after = before
        reason = "标的可参与组合决策"
        if code in context.disabled_symbols:
            after = 0.0
            reason = "标的已禁用"
        elif constraints.long_only and before < 0:
            after = 0.0
            reason = "long_only 禁止负目标权重"
        adjusted[code] = after
        audit.append(
            ConstraintAdjustment(
                constraint="investable_universe",
                symbol=code,
                before_value=before,
                after_value=after,
                limit=0.0 if after == 0 and before != 0 else None,
                passed=abs(before - after) <= MAX_WEIGHT_EPSILON,
                reason=reason,
            )
        )

    for code in sorted(adjusted):
        before = adjusted[code]
        after = float(np.sign(before)) * min(abs(before), constraints.max_weight_per_asset)
        adjusted[code] = after
        audit.append(
            ConstraintAdjustment(
                constraint="max_weight_per_asset",
                symbol=code,
                before_value=abs(before),
                after_value=abs(after),
                limit=constraints.max_weight_per_asset,
                passed=abs(before) <= constraints.max_weight_per_asset + MAX_WEIGHT_EPSILON,
                reason="单标的绝对权重截断",
            )
        )

    sleeve_for = {code: context.sleeve_map.get(code, f"unclassified:{code}") for code in adjusted}
    sleeve_names = set(sleeve_for.values())
    for sleeve in sorted(sleeve_names):
        members = [code for code in sorted(adjusted) if sleeve_for[code] == sleeve]
        before = sum(abs(adjusted[code]) for code in members)
        after = before
        if before > constraints.max_weight_per_sleeve + MAX_WEIGHT_EPSILON:
            scale = constraints.max_weight_per_sleeve / before
            for code in members:
                adjusted[code] *= scale
            after = constraints.max_weight_per_sleeve
        audit.append(
            ConstraintAdjustment(
                constraint="max_weight_per_sleeve",
                symbol=sleeve,
                before_value=before,
                after_value=after,
                limit=constraints.max_weight_per_sleeve,
                passed=before <= constraints.max_weight_per_sleeve + MAX_WEIGHT_EPSILON,
                reason=f"sleeve={sleeve} 绝对权重合计",
            )
        )

    gross_before = sum(abs(weight) for weight in adjusted.values())
    gross_after = gross_before
    gross_limit = constraints.max_investable_weight
    if gross_before > gross_limit + MAX_WEIGHT_EPSILON:
        scale = gross_limit / gross_before
        adjusted = {code: weight * scale for code, weight in adjusted.items()}
        gross_after = gross_limit
    audit.append(
        ConstraintAdjustment(
            constraint="gross_leverage",
            symbol=None,
            before_value=gross_before,
            after_value=gross_after,
            limit=gross_limit,
            passed=gross_before <= gross_limit + MAX_WEIGHT_EPSILON,
            reason="实际 gross exposure 不得超过杠杆/现金共同上限",
        )
    )

    return ConstraintApplication(
        weights={
            code: float(weight)
            for code, weight in adjusted.items()
            if abs(weight) > MAX_WEIGHT_EPSILON
        },
        adjustments=tuple(audit),
    )


def _signal_strengths(signals: list[Signal], tickers: list[str]) -> dict[str, float]:
    """按 score*confidence 聚合重复信号并做绝对值归一化。"""
    combined = dict.fromkeys(tickers, 0.0)
    for signal in signals:
        if signal.symbol in combined:
            combined[signal.symbol] += signal.score * signal.confidence
    return combined


def _cash_weight(weights: dict[str, float], constraints: PortfolioConstraints) -> float:
    if constraints.long_only and constraints.max_leverage <= 1 + MAX_WEIGHT_EPSILON:
        return max(constraints.min_cash_buffer, 1.0 - sum(weights.values()))
    return constraints.min_cash_buffer


# --------------------------------------------------------------------------- 等权


class EqualWeightAllocator:
    """等权分配器。

    所有候选标的获得相同权重 ``1/n``(截断后满足约束)。
    不依赖协方差矩阵;无信号的标的权重为 0。
    """

    name: str = "equal_weight"

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
        *,
        context: AllocationContext | None = None,
    ) -> TargetWeight:
        allocation_context = context or AllocationContext()
        tickers = _candidate_tickers(signals, None, constraints, allocation_context)
        if not tickers:
            raise AllocationError("无可用标的(信号为空或与协方差无交集)")

        strengths = _signal_strengths(signals, tickers)
        magnitude_total = sum(abs(strengths[ticker]) for ticker in tickers)
        if magnitude_total <= MAX_WEIGHT_EPSILON:
            raise AllocationError("信号在冲突合并后为中性")
        raw = {
            ticker: float(strengths[ticker] / magnitude_total) for ticker in tickers
        }
        application = apply_portfolio_constraints(raw, constraints, context=allocation_context)
        capped = application.weights
        cash = _cash_weight(capped, constraints)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
            max_leverage=constraints.max_leverage,
            long_only=constraints.long_only,
        )


# ----------------------------------------------------------------------- 逆波动率


class InverseVolatilityAllocator:
    """逆波动率分配器。

    权重正比于 ``1/sigma_i``,低波动标的获得更高权重。
    使用协方差矩阵对角线上的波动率;缺数标的权重为 0。
    """

    name: str = "inverse_volatility"

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
        *,
        context: AllocationContext | None = None,
    ) -> TargetWeight:
        allocation_context = context or AllocationContext()
        tickers = _candidate_tickers(signals, covariance, constraints, allocation_context)
        if not tickers:
            raise AllocationError("无可用标的")

        if covariance is None:
            raise AllocationError("逆波动率分配需要协方差矩阵")

        vols = covariance.volatilities
        vol_map = dict(zip(covariance.tickers, vols, strict=True))

        strengths = _signal_strengths(signals, tickers)
        inv_vols: dict[str, float] = {}
        for t in tickers:
            vol = vol_map.get(t, 0.0)
            if vol > MAX_WEIGHT_EPSILON:
                inv_vols[t] = abs(strengths[t]) / vol
            else:
                inv_vols[t] = 0.0

        total = sum(inv_vols.values())
        if total <= MAX_WEIGHT_EPSILON:
            raise AllocationError("所有标的波动率为零,无法逆波动率分配")

        raw = {
            ticker: float(
                float(np.sign(strengths[ticker])) * inv_vols[ticker] / total
            )
            for ticker in tickers
        }
        application = apply_portfolio_constraints(raw, constraints, context=allocation_context)
        capped = application.weights
        cash = _cash_weight(capped, constraints)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
            max_leverage=constraints.max_leverage,
            long_only=constraints.long_only,
        )


# --------------------------------------------------------------------------- ERC


@dataclass(frozen=True, slots=True)
class _ErcState:
    """ERC 迭代内部状态。"""

    weights: npt.NDArray[np.float64]
    tickers: list[str]
    n_iterations: int
    converged: bool
    dispersion: float


def _risk_contributions(
    weights: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """计算各标的的边际风险贡献(MRC)。

    RC_i = w_i * (Σw)_i / sqrt(w'Σw)

    ERC 的目标是所有 RC_i 相等。
    """
    portfolio_var = float(weights @ cov @ weights)
    if portfolio_var <= MAX_WEIGHT_EPSILON:
        return np.zeros_like(weights)
    mrc = cov @ weights
    return np.asarray(weights * mrc / np.sqrt(portfolio_var), dtype=np.float64)


def _erc_objective(
    weights: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> float:
    """ERC 目标函数:RC 之间的方差(越小越接近等贡献)。"""
    rc = _risk_contributions(weights, cov)
    if rc.sum() <= MAX_WEIGHT_EPSILON:
        return float("inf")
    rc_norm = rc / rc.sum()
    return float(np.sum((rc_norm - rc_norm.mean()) ** 2))


def _solve_erc(
    cov: npt.NDArray[np.float64],
    tickers: list[str],
    *,
    max_iter: int = DEFAULT_MAX_ITER,
    tol: float = DEFAULT_TOLERANCE,
) -> _ErcState:
    """迭代求解 ERC 权重。

    使用迭代投影法:
    1. 初始权重 = 1/sigma(逆波动率)。
    2. 每轮按风险贡献偏差调整:RC 高的减权、RC 低的加权。
    3. 投影到单纯形(权重 ≥ 0,和 = 1)。
    4. 收敛条件:目标函数 < tol 或迭代数超限。
    """
    n = len(tickers)
    if n == 1:
        return _ErcState(
            weights=np.array([1.0]),
            tickers=tickers,
            n_iterations=0,
            converged=True,
            dispersion=0.0,
        )

    vols = np.sqrt(np.diag(cov))
    vols_safe = np.where(vols > MAX_WEIGHT_EPSILON, vols, 1.0)
    w = 1.0 / vols_safe
    w[w < 0] = 0.0
    total = w.sum()
    w = np.full(n, 1.0 / n) if total <= MAX_WEIGHT_EPSILON else w / total

    prev_obj = _erc_objective(w, cov)
    step = 1.0

    for iteration in range(max_iter):
        rc = _risk_contributions(w, cov)
        rc_sum = rc.sum()
        if rc_sum <= MAX_WEIGHT_EPSILON:
            break

        target_rc = rc_sum / n
        ratios = np.where(
            rc > MAX_WEIGHT_EPSILON, target_rc / np.maximum(rc, MAX_WEIGHT_EPSILON), 1.0
        )
        ratios = np.clip(ratios, 0.5, 2.0)

        new_w = w * (1.0 - step + step * ratios)
        new_w = np.maximum(new_w, 0.0)
        total_new = new_w.sum()
        new_w = new_w / total_new if total_new > MAX_WEIGHT_EPSILON else np.full(n, 1.0 / n)

        new_obj = _erc_objective(new_w, cov)
        if new_obj < prev_obj:
            w = new_w
            prev_obj = new_obj
        else:
            step *= 0.5
            if step < tol:
                break

        if prev_obj < tol:
            return _ErcState(
                weights=w,
                tickers=tickers,
                n_iterations=iteration + 1,
                converged=True,
                dispersion=prev_obj,
            )

    return _ErcState(
        weights=w,
        tickers=tickers,
        n_iterations=max_iter,
        converged=prev_obj < tol,
        dispersion=prev_obj,
    )


class ErcAllocator:
    """等风险贡献(ERC)分配器。

    目标:每个标的对组合总风险的贡献相等。使用迭代法求解,
    初始权重为逆波动率,逐步调整使风险贡献均衡。

    当协方差矩阵不可用或迭代不收敛时 raise ``AllocationError``。
    """

    name: str = "erc"

    def __init__(self, *, max_iter: int = DEFAULT_MAX_ITER, tol: float = DEFAULT_TOLERANCE) -> None:
        self._max_iter = max_iter
        self._tol = tol

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
        *,
        context: AllocationContext | None = None,
    ) -> TargetWeight:
        allocation_context = context or AllocationContext()
        tickers = _candidate_tickers(signals, covariance, constraints, allocation_context)
        if not tickers:
            raise AllocationError("无可用标的")

        if covariance is None:
            raise AllocationError("ERC 分配需要协方差矩阵")

        idx = [covariance.tickers.index(t) for t in tickers]
        sub_cov = covariance.matrix[np.ix_(idx, idx)]

        state = _solve_erc(
            sub_cov,
            tickers,
            max_iter=self._max_iter,
            tol=self._tol,
        )

        if not state.converged and state.dispersion > 1e-4:
            raise AllocationError(f"ERC 迭代未收敛: dispersion={state.dispersion:.2e} > 1e-4")

        strengths = _signal_strengths(signals, tickers)
        raw_magnitudes = {
            ticker: weight * abs(strengths[ticker])
            for ticker, weight in zip(tickers, state.weights.tolist(), strict=True)
        }
        magnitude_total = sum(raw_magnitudes.values())
        if magnitude_total <= MAX_WEIGHT_EPSILON:
            raise AllocationError("信号在冲突合并后为中性")
        raw = {
            ticker: float(
                float(np.sign(strengths[ticker]))
                * raw_magnitudes[ticker]
                / magnitude_total
            )
            for ticker in tickers
        }
        application = apply_portfolio_constraints(raw, constraints, context=allocation_context)
        capped = application.weights
        cash = _cash_weight(capped, constraints)

        return TargetWeight(
            weights=capped,
            as_of=signals[0].timestamp,
            strategy_id=signals[0].strategy_id,
            cash_buffer=cash,
            max_leverage=constraints.max_leverage,
            long_only=constraints.long_only,
            covariance_version=f"lw_delta{covariance.shrinkage:.4f}",
        )


# ---------------------------------------------------------------------- 直权重


class DirectWeightsAllocator:
    """直目标权重分配器(issue #218 ``user_code`` 策略)。

    ``signal.score`` 的语义即目标权重本身(沙箱 ``strategy.decide`` 输出
    的权重映射为信号分数),分配器**不做任何再缩放** —— 权重和可以 < 1
    (余下为现金)或 = 0(空仓观望)。硬约束(long-only 负权重截断、单资产
    上限、sleeve、gross 上限)仍由 :func:`apply_portfolio_constraints` 与
    build_portfolio 的后续层执行并逐项记审计(超约束截断可见)。

    与其他分配器的关键差异:其余方法把 score 当「强度」经归一/缩放产生
    权重;本方法信任 score 的绝对值 —— 因此只应由 user_code 管线使用,
    spec 的 allocation_method 不暴露此取值。
    """

    name: str = "direct_weights"

    def allocate(
        self,
        signals: list[Signal],
        covariance: CovarianceEstimate | None,
        constraints: PortfolioConstraints,
        *,
        context: AllocationContext | None = None,
    ) -> TargetWeight:
        allocation_context = context or AllocationContext()
        first = signals[0]
        weights: dict[str, float] = {}
        for signal in signals:
            if signal.symbol in allocation_context.disabled_symbols:
                continue
            weights[signal.symbol] = float(signal.score * signal.confidence)
        gross = sum(abs(weight) for weight in weights.values())
        return TargetWeight(
            weights=weights,
            as_of=first.timestamp,
            strategy_id=first.strategy_id,
            cash_buffer=max(0.0, 1.0 - gross),
            max_leverage=max(1.0, gross),
            long_only=constraints.long_only,
        )


# --------------------------------------------------------------------------- 工厂


_ALLOCATOR_REGISTRY: dict[
    str,
    type[EqualWeightAllocator]
    | type[InverseVolatilityAllocator]
    | type[ErcAllocator]
    | type[DirectWeightsAllocator],
] = {
    "equal_weight": EqualWeightAllocator,
    "inverse_volatility": InverseVolatilityAllocator,
    "erc": ErcAllocator,
    "direct_weights": DirectWeightsAllocator,
}


def make_allocator(method: str) -> Allocator:
    """按名称构造分配器。

    支持的方法:``"equal_weight"`` / ``"inverse_volatility"`` / ``"erc"`` /
    ``"direct_weights"``(user_code 专用,score 即权重,不再缩放)。
    未知方法 raise ``AllocationError``。
    """
    cls = _ALLOCATOR_REGISTRY.get(method)
    if cls is None:
        raise AllocationError(f"未知分配方法: {method};可选: {', '.join(_ALLOCATOR_REGISTRY)}")
    return cls()


__all__ = [
    "AllocationContext",
    "AllocationError",
    "Allocator",
    "ConstraintAdjustment",
    "ConstraintApplication",
    "DirectWeightsAllocator",
    "EqualWeightAllocator",
    "ErcAllocator",
    "InverseVolatilityAllocator",
    "apply_portfolio_constraints",
    "make_allocator",
]
