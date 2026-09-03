"""最大 IR(切点)分配器 —— 纯 numpy 投影梯度求解(issue #266)。

最大化事前信息比率 ``S(w) = μ̃'w / sqrt(w'Σw)``(μ̃ = 预期超额收益代理,
默认基准为现金,即经典切点组合 / 最大夏普),约束为 long-only 预算
``Σw = 1`` 加可选单标的上限 ``w_i <= cap``。

求解器选型(issue #266 关键决策):仓库数值依赖只有 numpy,**不引入
scipy / cvxpy** —— capped simplex 上的投影梯度上升 + Armijo 回溯线搜索
对该问题全局收敛(S 在 Σ ≻ 0 区域伪凹,一阶平稳点即全局最优),单纯形
投影有闭式算法(τ 平移 + 单调两分),精度由测试对解析切点解锁定。
收敛失败时 raise ``AllocationError`` —— 与 ERC 同语义,由 builder 既有
``covariance_failure_mode`` 分流(默认 fail_closed),不静默回退。

信号强度的语义:``score * confidence`` 聚合值作为**相对**预期超额收益
代理 —— IR 目标对 μ̃ 的正尺度变换不变,只有相对大小与符号影响结果;
负 μ̃ 标的在 long-only 形态下自动获得 0 权重。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from finboard_backtest.portfolio.allocators import (
    AllocationContext,
    AllocationError,
    _candidate_tickers,
    _cash_weight,
    _signal_strengths,
    apply_portfolio_constraints,
)
from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    PortfolioConstraints,
    Signal,
    TargetWeight,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate

DEFAULT_MAX_ITER = 2000
"""投影梯度最大迭代次数;O(n^2)/迭代,组合规模下开销可忽略。"""

DEFAULT_TOLERANCE = 1e-6
"""最优性残差容忍:归一化映射梯度残差低于该值视为收敛。"""

_ARMIJO_HALVINGS = 40
_ARMIJO_MIN_STEP = 1e-16


@dataclass(frozen=True, slots=True)
class MaxIrSolution:
    """最大 IR 求解结果。

    属性:
        weights: 满足预算与上限约束的权重向量(与传入标的顺序一致)。
        objective: 最优目标值 μ̃'w / sqrt(w'Σw)(μ̃ 已做尺度归一)。
        iterations: 实际迭代(更新)次数。
        converged: 最优性残差是否低于容忍度。
        residual: 归一化映射梯度残差(一阶最优性度量)。
    """

    weights: npt.NDArray[np.float64]
    objective: float
    iterations: int
    converged: bool
    residual: float


def project_onto_capped_simplex(
    vector: npt.NDArray[np.float64],
    cap: float,
) -> npt.NDArray[np.float64]:
    """投影到 {w: w >= 0, w <= cap, Σw = 1}(欧氏距离意义)。

    解为 ``w_i = clip(v_i - τ, 0, cap)``;``Σ clip(v - τ)`` 对 τ 严格单调
    递减,故两分求 τ 精确到机器精度,完全确定。
    """
    n = np.asarray(vector).shape[0]
    if cap * n < 1.0 - 1e-12:
        raise AllocationError(
            f"单标的上限不可行: cap={cap:.6f} < 1/n={1.0 / n:.6f}"
        )
    v = np.clip(np.asarray(vector, dtype=np.float64), -1e12, 1e12)

    def total(shift: float) -> float:
        return float(np.clip(v - shift, 0.0, cap).sum())

    lower = float(v.min()) - cap - 1.0
    upper = float(v.max()) + 1.0
    for _ in range(100):
        middle = (lower + upper) / 2.0
        if total(middle) > 1.0:
            lower = middle
        else:
            upper = middle
    return np.clip(v - (lower + upper) / 2.0, 0.0, cap)


def _objective(
    weights: npt.NDArray[np.float64],
    mu: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> float:
    excess = float(mu @ weights)
    variance = float(weights @ cov @ weights)
    if variance <= MAX_WEIGHT_EPSILON:
        return float("-inf")
    return excess / float(np.sqrt(variance))


def _gradient(
    weights: npt.NDArray[np.float64],
    mu: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """dS/dw = (mu·sigma_p^2 - (mu'w)·Sigma·w) / sigma_p^3(商法则)。"""
    cov_w = cov @ weights
    variance = float(weights @ cov_w)
    sigma = float(np.sqrt(variance))
    if variance <= MAX_WEIGHT_EPSILON or sigma <= 0.0:
        return np.zeros_like(weights)
    excess = float(mu @ weights)
    return (mu * variance - excess * cov_w) / (variance * sigma)


def _stationarity_residual(
    weights: npt.NDArray[np.float64],
    gradient: npt.NDArray[np.float64],
    cap: float,
) -> float:
    """尺度不变的一阶平稳性残差。

    ``Pi(w + alpha·g) = w`` 等价于 ``<g, x - w> <= 0 对所有 x in C``(g 落在
    负法锥),该条件与 alpha > 0 无关。残差取
    ``||Pi(w + g) - w||_inf / max(1, ||g||_inf)``:梯度大时按梯度尺度归一
    (等价单位方向探测),梯度趋零(内点最优)时残差随梯度本身趋零,
    不会被浮点噪声主导。
    """
    norm = float(np.abs(gradient).max())
    if norm <= MAX_WEIGHT_EPSILON:
        return 0.0
    probe = project_onto_capped_simplex(weights + gradient, cap)
    return float(np.abs(probe - weights).max()) / max(1.0, norm)


def _initial_weights(
    mu: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
    cap: float,
) -> npt.NDArray[np.float64]:
    """初始化:无约束切点 y ∝ Σ⁻¹μ̃ 截负归一后投影;病态时回退逆波动率。

    切点解对正尺度变换不变,正分量先按和归一再投影(单纯形投影是
    平移截断,不会把正向量的方向比例保留成预算分配)。仅为加速,
    不决定正确性 —— 投影梯度对任意可行初始点全局收敛。
    """
    try:
        y = np.linalg.solve(cov, mu)
        if not np.isfinite(y).all():
            raise np.linalg.LinAlgError("切点初始化非有限")
    except np.linalg.LinAlgError:
        vols = np.sqrt(np.diag(cov))
        y = np.where(
            vols > MAX_WEIGHT_EPSILON,
            1.0 / np.where(vols > MAX_WEIGHT_EPSILON, vols, 1.0),
            0.0,
        )
    y = np.clip(y, 0.0, None)
    total = float(y.sum())
    if total > MAX_WEIGHT_EPSILON:
        y = y / total
    return project_onto_capped_simplex(y, cap)


def solve_max_ir(
    mu: npt.NDArray[np.float64],
    cov: npt.NDArray[np.float64],
    *,
    cap: float | None = None,
    max_iter: int = DEFAULT_MAX_ITER,
    tolerance: float = DEFAULT_TOLERANCE,
) -> MaxIrSolution:
    """投影梯度上升求解 capped simplex 上的最大 IR 组合。

    ``mu`` 为预期**超额**收益代理(可为任意正尺度,目标对正尺度变换
    不变,内部按最大绝对值归一保证数值稳定)。``cap`` 为单标的权重上限
    (``None`` = 1.0 即纯单纯形)。确定性输入 → 确定性输出,无随机成分。
    收敛失败(迭代耗尽或线搜索无改进且残差超限)返回 ``converged=False``,
    由调用方决定失败语义。
    """
    mu = np.asarray(mu, dtype=np.float64)
    cov = np.asarray(cov, dtype=np.float64)
    n = mu.shape[0]
    if n == 0:
        raise AllocationError("无可用标的")
    if cov.shape != (n, n):
        raise AllocationError("收益向量与协方差矩阵维度不匹配")
    limit = 1.0 if cap is None else float(cap)
    if not (MAX_WEIGHT_EPSILON < limit <= 1.0 + MAX_WEIGHT_EPSILON):
        raise AllocationError(f"单标的上限必须在 (0, 1],实际 {limit}")
    if max_iter <= 0:
        raise AllocationError("max_iter 必须为正")

    scale = float(np.abs(mu).max())
    if not np.isfinite(scale) or scale <= MAX_WEIGHT_EPSILON:
        raise AllocationError("信号在冲突合并后为中性,无预期超额收益方向")
    mu = mu / scale

    sym = (cov + cov.T) / 2.0
    min_eigenvalue = float(np.linalg.eigvalsh(sym).min())
    eigenvalue_tolerance = (
        np.finfo(np.float64).eps * max(1.0, float(np.abs(cov).max())) * n
    )
    if min_eigenvalue <= eigenvalue_tolerance:
        raise AllocationError(
            f"协方差矩阵奇异或非正定: min_eigenvalue={min_eigenvalue:.3e}"
        )

    weights = _initial_weights(mu, cov, limit)
    objective = _objective(weights, mu, cov)
    step = 0.0
    iterations = 0

    for iteration in range(1, max_iter + 1):
        gradient = _gradient(weights, mu, cov)
        residual = _stationarity_residual(weights, gradient, limit)
        if residual <= tolerance:
            return MaxIrSolution(
                weights=weights,
                objective=objective,
                iterations=iterations,
                converged=True,
                residual=residual,
            )

        if step <= 0.0:
            # 初始/重置步长:令试探位移的 ∞ 范数约 0.5,避免首步过大。
            gradient_inf = float(np.abs(gradient).max())
            step = 0.5 / gradient_inf if gradient_inf > 0 else 1.0

        accepted = False
        trial_step = step
        for _ in range(_ARMIJO_HALVINGS):
            candidate = project_onto_capped_simplex(
                weights + trial_step * gradient, limit
            )
            candidate_objective = _objective(candidate, mu, cov)
            if candidate_objective > objective:
                weights = candidate
                objective = candidate_objective
                step = min(trial_step * 2.0, 1e12)
                accepted = True
                iterations = iteration
                break
            trial_step *= 0.5
            if trial_step < _ARMIJO_MIN_STEP:
                break
        if not accepted:
            # 线搜索耗尽仍无改进:已到数值可达的最优,按残差判定收敛性。
            break

    gradient = _gradient(weights, mu, cov)
    residual = _stationarity_residual(weights, gradient, limit)
    return MaxIrSolution(
        weights=weights,
        objective=objective,
        iterations=iterations,
        converged=residual <= tolerance,
        residual=residual,
    )


class MaxIrAllocator:
    """最大 IR(切点)分配器(issue #266)。

    基于 LW 协方差子阵与信号强度代理的预期超额收益,求解 long-only
    预算 + 可选单标的上限下的事前 IR 最大化组合。协方差缺失、信号全中性
    / 全为负、协方差非正定或迭代不收敛时 raise ``AllocationError`` ——
    与 ERC 同语义,由 builder 按 ``covariance_failure_mode`` 分流。
    """

    name: str = "max_ir"

    def __init__(
        self,
        *,
        max_iter: int = DEFAULT_MAX_ITER,
        tol: float = DEFAULT_TOLERANCE,
        risk_free_daily: float = 0.0,
    ) -> None:
        self._max_iter = max_iter
        self._tol = tol
        self._risk_free_daily = risk_free_daily

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
            raise AllocationError("最大 IR 分配需要协方差矩阵")

        idx = [covariance.tickers.index(t) for t in tickers]
        sub_cov = covariance.matrix[np.ix_(idx, idx)]

        strengths = _signal_strengths(signals, tickers)
        mu = np.array(
            [strengths[ticker] - self._risk_free_daily for ticker in tickers],
            dtype=np.float64,
        )
        if not (mu > MAX_WEIGHT_EPSILON).any():
            raise AllocationError("无正预期超额收益信号,最大 IR 组合退化(long-only)")

        # builder 以 relaxed 约束(cap=1)调用分配器,真实单标的上限在
        # 约束管线中执行并逐项审计;直连使用时若声明了上限则求解器内嵌。
        cap = (
            constraints.max_weight_per_asset
            if constraints.max_weight_per_asset < 1.0 - MAX_WEIGHT_EPSILON
            else None
        )
        solution = solve_max_ir(
            mu,
            sub_cov,
            cap=cap,
            max_iter=self._max_iter,
            tolerance=self._tol,
        )
        if not solution.converged:
            raise AllocationError(
                "最大 IR 迭代未收敛:"
                f" residual={solution.residual:.3e} > {self._tol:.1e},"
                f" iterations={solution.iterations}"
            )

        raw = {
            ticker: float(weight)
            for ticker, weight in zip(tickers, solution.weights.tolist(), strict=True)
            if weight > MAX_WEIGHT_EPSILON
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


__all__ = [
    "DEFAULT_MAX_ITER",
    "DEFAULT_TOLERANCE",
    "MaxIrAllocator",
    "MaxIrSolution",
    "project_onto_capped_simplex",
    "solve_max_ir",
]
